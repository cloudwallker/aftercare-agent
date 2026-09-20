"""API 只接收和读取任务，不启动 Worker 或发起退款。"""

from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException

from aftercare import approvals, tasks
from aftercare.auth import authenticate
from aftercare.commands import DomainError
from aftercare.config import ApiSettings, load_settings
from aftercare.contracts import Actor, ApprovalDecision, CreateTask, ReassessRequest
from aftercare.db import Database, create_database
from aftercare.events import event_batch, stream_events


def create_app(
    settings: ApiSettings | None = None,
    *,
    database: Database | None = None,
    actors: dict[str, Actor] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    if not isinstance(settings, ApiSettings) or settings.role != "api":
        raise ValueError("API 必须使用 api 角色")
    owns_database = database is None
    database = database or create_database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if owns_database:
                await database.engine.dispose()

    app = FastAPI(title="Aftercare Agent", lifespan=lifespan)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def error_response(request, status, code, message):
        rid = getattr(request.state, "request_id", str(uuid4()))
        return JSONResponse(
            {"error": {"code": code, "message": message, "request_id": rid}},
            status_code=status,
            headers={
                "X-Request-ID": rid,
                **({"WWW-Authenticate": "Bearer"} if status == 401 else {}),
            },
        )

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return error_response(request, exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error_response(request, 422, "INVALID_REQUEST", "请求参数不符合契约。")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error_response(request, exc.status_code, "HTTP_ERROR", "请求未被接受。")

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        return error_response(request, 500, "INTERNAL_ERROR", "服务暂时无法处理请求。")

    async def actor(authorization: Annotated[str | None, Header()] = None) -> Actor:
        return authenticate(settings, authorization, actors)

    @app.get("/health/live")
    async def live():
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready():
        try:
            async with database.sessions() as session:
                versions = (
                    await session.scalars(text("SELECT version_num FROM public.alembic_version"))
                ).all()
                if versions != ["0001_initial"]:
                    raise RuntimeError("迁移版本未就绪")
                for name in (
                    "tasks",
                    "steps",
                    "approvals",
                    "refund_operations",
                    "events",
                    "command_requests",
                ):
                    await session.execute(text(f"SELECT 1 FROM app.{name} LIMIT 0"))
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return {"status": "ready"}

    @app.post("/v1/tasks", status_code=201)
    async def create(
        data: CreateTask,
        identity: Annotated[Actor, Depends(actor)],
        idempotency_key: Annotated[str | None, Header()] = None,
    ):
        result = await tasks.create_task(database, identity, data, idempotency_key)
        return JSONResponse(
            result.model_dump(mode="json"),
            status_code=201,
            headers={"Idempotency-Replayed": "true"} if result.replayed else None,
        )

    @app.get("/v1/tasks")
    async def listing(
        identity: Annotated[Actor, Depends(actor)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ):
        return await tasks.list_tasks(database, identity, limit, cursor)

    @app.get("/v1/tasks/{task_id}")
    async def detail(task_id: UUID, identity: Annotated[Actor, Depends(actor)]):
        return await tasks.get_task(database, identity, task_id)

    @app.get("/v1/tasks/{task_id}/steps")
    async def steps(
        task_id: UUID,
        identity: Annotated[Actor, Depends(actor)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ):
        return await tasks.list_steps(database, identity, task_id, limit, cursor)

    @app.get("/v1/tasks/{task_id}/approvals")
    async def history(task_id: UUID, identity: Annotated[Actor, Depends(actor)]):
        return await approvals.list_approvals(database, identity, task_id)

    @app.post("/v1/tasks/{task_id}/reassess", status_code=202)
    async def reassess(
        task_id: UUID,
        data: ReassessRequest,
        identity: Annotated[Actor, Depends(actor)],
        idempotency_key: Annotated[str | None, Header()] = None,
    ):
        result = await approvals.reassess(database, identity, task_id, data, idempotency_key)
        return JSONResponse(
            result.model_dump(mode="json"),
            status_code=202,
            headers={"Idempotency-Replayed": "true"} if result.replayed else None,
        )

    @app.post("/v1/approvals/{approval_id}/decision")
    async def decision(
        approval_id: UUID,
        data: ApprovalDecision,
        identity: Annotated[Actor, Depends(actor)],
        idempotency_key: Annotated[str | None, Header()] = None,
    ):
        result = await approvals.decide_approval(
            database, identity, approval_id, data, idempotency_key
        )
        return JSONResponse(
            result.model_dump(mode="json"),
            headers={"Idempotency-Replayed": "true"} if result.replayed else None,
        )

    @app.get("/v1/tasks/{task_id}/events", response_class=StreamingResponse)
    async def events(
        task_id: UUID,
        request: Request,
        identity: Annotated[Actor, Depends(actor)],
        last_event_id: Annotated[str | None, Header()] = None,
    ):
        value = "0" if last_event_id is None else last_event_id
        if not value.isascii() or not value.isdigit() or len(value) > 19 or int(value) > 2**63 - 1:
            raise DomainError(422, "INVALID_EVENT_ID", "Last-Event-ID 必须为非负整数。")
        cursor = int(value)
        _, high, _ = await event_batch(database, identity, task_id, cursor)
        if cursor > high:
            raise DomainError(409, "EVENT_CURSOR_AHEAD", "事件游标超出当前任务范围。")
        return StreamingResponse(
            stream_events(database, identity, task_id, cursor, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
