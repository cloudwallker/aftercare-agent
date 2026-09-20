"""任务创建与只读视图；不访问模型或商家 HTTP。"""

import base64
import json
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select, tuple_

from aftercare.auth import require_client
from aftercare.commands import DomainError, run_command
from aftercare.contracts import Actor, CreateResult, CreateTask
from aftercare.db import Database
from aftercare.events import append_event
from aftercare.models import Approval, RefundOperation, Step, Task


async def create_task(database: Database, actor: Actor, data: CreateTask, key: str) -> CreateResult:
    require_client(actor)

    async def action(session):
        task = Task(
            id=uuid4(), customer_id=actor.subject, order_id=data.order_id, message=data.message
        )
        session.add(task)
        await session.flush()
        await append_event(session, task, "task.created", {})
        return 201, {"task_id": str(task.id), "status": "queued"}

    result = await run_command(
        database, actor, "/v1/tasks", key, data.model_dump(mode="json"), action
    )
    return CreateResult(**result.body, replayed=result.replayed)


def visible_query(actor: Actor):
    query = select(Task)
    if actor.role == "client":
        query = query.where(Task.customer_id == actor.subject)
    elif actor.role != "reviewer":
        raise DomainError(403, "FORBIDDEN", "无访问权限。")
    return query


async def visible_task(session, actor: Actor, task_id: UUID, *, lock=False) -> Task:
    query = visible_query(actor).where(Task.id == task_id)
    if lock:
        query = query.with_for_update(read=True)
    task = await session.scalar(query)
    if task is None:
        raise DomainError(404, "TASK_NOT_FOUND", "未找到工单。")
    return task


def fields(row, names):
    return {name: getattr(row, name) for name in names.split()}


SUMMARY_FIELDS = "id order_id status phase generation created_at updated_at"


async def get_task(database: Database, actor: Actor, task_id: UUID) -> dict:
    async with database.sessions.begin() as session:
        task = await visible_task(session, actor, task_id, lock=True)
        approval = await session.scalar(
            select(Approval).where(
                Approval.task_id == task_id, Approval.generation == task.generation
            )
        )
        operation = await session.scalar(
            select(RefundOperation).where(RefundOperation.task_id == task_id)
        )
        return {
            **fields(task, SUMMARY_FIELDS),
            "message": task.message,
            "last_event_seq": task.last_event_seq,
            "approval": fields(
                approval,
                "id task_id generation status payload payload_hash summary "
                "expires_at decided_by decided_at",
            )
            if approval
            else None,
            "operation": fields(
                operation,
                "id operation_key generation status dispatch_count "
                "reconcile_count merchant_refund_id last_error_code",
            )
            if operation
            else None,
            "result": task.result if task.status == "succeeded" else None,
            "error": {"code": task.error_code, "message": task.error_detail or "任务未完成。"}
            if task.error_code
            else None,
        }


def encode_cursor(values: list) -> str:
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode()


def decode_cursor(value: str, *, steps=False) -> tuple:
    try:
        if len(value) > 512:
            raise ValueError
        values = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if not isinstance(values, list) or len(values) != 2:
            raise ValueError
        if steps:
            if any(type(v) is not int or v < 1 for v in values) or values[0] > 3:
                raise ValueError
            return tuple(values)
        if not all(isinstance(value, str) for value in values):
            raise ValueError
        timestamp, uid = datetime.fromisoformat(values[0]), UUID(values[1])
        if timestamp.tzinfo is None:
            raise ValueError
        return timestamp, uid
    except (ValueError, TypeError, OverflowError):
        raise DomainError(422, "INVALID_CURSOR", "分页游标无效。") from None


def validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError(422, "INVALID_LIMIT", "分页大小须为 1～100。")


async def list_tasks(database: Database, actor: Actor, limit=20, cursor=None) -> dict:
    validate_limit(limit)
    query = visible_query(actor)
    if cursor is not None:
        query = query.where(tuple_(Task.created_at, Task.id) < decode_cursor(cursor))
    async with database.sessions() as session:
        rows = (
            await session.scalars(
                query.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit + 1)
            )
        ).all()
        page = rows[:limit]
        return {
            "items": [fields(row, SUMMARY_FIELDS) for row in page],
            "next_cursor": encode_cursor([page[-1].created_at.isoformat(), str(page[-1].id)])
            if len(rows) > limit
            else None,
        }


async def list_steps(
    database: Database, actor: Actor, task_id: UUID, limit=20, cursor=None
) -> dict:
    validate_limit(limit)
    query = select(Step).where(Step.task_id == task_id)
    if cursor is not None:
        query = query.where(
            tuple_(Step.generation, Step.step_no) > decode_cursor(cursor, steps=True)
        )
    async with database.sessions() as session:
        await visible_task(session, actor, task_id)
        rows = (
            await session.scalars(query.order_by(Step.generation, Step.step_no).limit(limit + 1))
        ).all()
        page = rows[:limit]
        return {
            "items": [
                fields(
                    row,
                    "id generation step_no kind name status attempt_count input "
                    "output error_code started_at completed_at",
                )
                for row in page
            ],
            "next_cursor": encode_cursor([page[-1].generation, page[-1].step_no])
            if len(rows) > limit
            else None,
        }
