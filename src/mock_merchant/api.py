"""仅面向内部服务的模拟商家 HTTP 接口。"""

import secrets
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from aftercare.config import MerchantSettings, load_settings
from aftercare.contracts import RefundRequest
from aftercare.db import Database, create_database
from mock_merchant import ledger
from mock_merchant.models import Order


def create_app(
    settings: MerchantSettings | None = None,
    *,
    database: Database | None = None,
    testing: bool = False,
    fault_mode: str | None = None,
) -> FastAPI:
    if fault_mode is not None and not testing:
        raise ValueError("故障模式必须显式启用 testing")
    if fault_mode not in {None, "commit_then_503_once"}:
        raise ValueError("不支持的 fault_mode")
    settings = settings if settings is not None else load_settings()
    if not isinstance(settings, MerchantSettings) or settings.role != "merchant":
        raise ValueError("商家服务必须使用 merchant 角色")
    owns_database = database is None
    database = database if database is not None else create_database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            if owns_database:
                await database.engine.dispose()

    app = FastAPI(title="模拟商家", lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse(
            {
                "error": {
                    "code": exc.detail,
                    "message": "商家请求未被接受。",
                    "request_id": str(uuid4()),
                }
            },
            status_code=exc.status_code,
            headers=exc.headers,
        )

    async def authenticate(authorization: Annotated[str | None, Header()] = None):
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            token.encode("utf-8"), settings.merchant_token.encode("utf-8")
        ):
            raise HTTPException(401, "UNAUTHORIZED", headers={"WWW-Authenticate": "Bearer"})

    protected = [Depends(authenticate)]

    def validate_key(value: str | None) -> str:
        if (
            value is None
            or not 1 <= len(value) <= 128
            or any(not 32 <= ord(character) <= 126 for character in value)
        ):
            raise HTTPException(422, "INVALID_IDEMPOTENCY_KEY")
        return value

    async def visible_order(order_id: str, customer_id: str) -> Order:
        async with database.sessions() as session:
            order = await session.scalar(
                select(Order).where(Order.id == order_id, Order.customer_id == customer_id)
            )
            if order is None:
                raise HTTPException(404, "ORDER_NOT_FOUND")
            return order

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
                await session.execute(text("SELECT id FROM merchant.orders LIMIT 0"))
                await session.execute(
                    text("SELECT operation_key FROM merchant.refund_requests LIMIT 0")
                )
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return {"status": "ready"}

    @app.get("/orders/{order_id}", dependencies=protected)
    async def get_order(
        order_id: str, customer_id: Annotated[str, Query(min_length=1, max_length=128)]
    ):
        order = await visible_order(order_id, customer_id)
        return {
            "id": order.id,
            "customer_id": order.customer_id,
            "paid_amount_cents": order.paid_amount_cents,
            "currency": order.currency,
            "payment_status": order.payment_status,
            "refunded": order.refunded,
            "order_version": order.order_version,
        }

    @app.get("/orders/{order_id}/shipment", dependencies=protected)
    async def get_shipment(
        order_id: str, customer_id: Annotated[str, Query(min_length=1, max_length=128)]
    ):
        order = await visible_order(order_id, customer_id)
        return {
            "order_id": order.id,
            "shipment_status": order.shipment_status,
            "delay_days": order.delay_days,
            "order_version": order.order_version,
        }

    @app.post("/refunds", dependencies=protected)
    async def post_refund(
        payload: RefundRequest,
        idempotency_key: Annotated[str | None, Header()] = None,
    ):
        key = validate_key(idempotency_key)
        try:
            result = await ledger.refund(
                database, key, payload, lose_first_response=fault_mode == "commit_then_503_once"
            )
        except ledger.IdempotencyConflict:
            raise HTTPException(409, "IDEMPOTENCY_CONFLICT") from None
        if result.lose_response:
            raise HTTPException(503, "TEMPORARILY_UNAVAILABLE")
        return JSONResponse(
            result.body,
            status_code=result.http_status,
            headers={"Idempotency-Replayed": "true"} if result.replayed else None,
        )

    @app.get("/refunds/by-key/{key:path}", dependencies=protected)
    async def get_refund(key: str):
        body = await ledger.lookup(database, validate_key(key))
        if body is None:
            raise HTTPException(404, "REFUND_NOT_FOUND")
        return body

    return app
