"""以数据库时钟和行锁实现本地所有权；不保证外部 HTTP 只调用一次。"""

from datetime import timedelta

from sqlalchemy import and_, func, or_, select

from aftercare.contracts import Lease
from aftercare.db import Database
from aftercare.events import append_event
from aftercare.models import RefundOperation, Task


class LeaseLost(Exception):
    """失效 Worker 必须丢弃本地结果，不能重新续租复活。"""


async def locked_task(session, lease: Lease):
    task = await session.scalar(select(Task).where(Task.id == lease.task_id).with_for_update())
    now = await session.scalar(select(func.clock_timestamp()))
    if (
        task is None
        or task.status != "running"
        or task.lease_owner != lease.worker_id
        or task.lease_epoch != lease.epoch
        or task.lease_expires_at is None
        or task.lease_expires_at <= now
    ):
        raise LeaseLost
    return task, now


async def claim_next(database: Database, worker_id: str, lease_seconds: int = 20) -> Lease | None:
    if not worker_id or lease_seconds <= 0:
        raise ValueError("worker_id 和 lease_seconds 无效")
    async with database.sessions.begin() as session:
        task = await session.scalar(
            select(Task)
            .where(
                or_(
                    and_(Task.status == "queued", Task.next_run_at <= func.clock_timestamp()),
                    and_(Task.status == "running", Task.lease_expires_at <= func.clock_timestamp()),
                )
            )
            .order_by(Task.created_at, Task.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if task is None:
            return None
        now = await session.scalar(select(func.clock_timestamp()))
        recovered = task.status == "running"
        task.status, task.lease_owner = "running", worker_id
        task.lease_epoch += 1
        task.lease_expires_at = now + timedelta(seconds=lease_seconds)
        task.updated_at = now
        if task.phase == "investigate" and task.generation_started_at is None:
            task.generation_started_at = now
        await append_event(
            session,
            task,
            "task.recovered" if recovered else "task.claimed",
            {"lease_epoch": task.lease_epoch},
        )
        return Lease(task.id, worker_id, task.lease_epoch)


async def renew_lease(database: Database, lease: Lease, lease_seconds: int = 20) -> bool:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds 必须为正数")
    try:
        async with database.sessions.begin() as session:
            task, now = await locked_task(session, lease)
            task.lease_expires_at = now + timedelta(seconds=lease_seconds)
        return True
    except LeaseLost:
        return False


def clear_lease(task: Task) -> None:
    task.lease_owner = None
    task.lease_expires_at = None


async def schedule_retry(
    database: Database, lease: Lease, *, delay_seconds: float, error_code: str | None = None
) -> None:
    if not 0 <= delay_seconds <= 30:
        raise ValueError("重试退避须为 0～30 秒")
    async with database.sessions.begin() as session:
        task, now = await locked_task(session, lease)
        task.status = "queued"
        task.next_run_at = now + timedelta(seconds=delay_seconds)
        task.updated_at = now
        clear_lease(task)
        await append_event(session, task, "task.retry_scheduled", {"error_code": error_code})


async def fail_task(database: Database, lease: Lease, code: str, *, rejected=False) -> None:
    async with database.sessions.begin() as session:
        task, now = await locked_task(session, lease)
        operation = await session.scalar(
            select(RefundOperation).where(RefundOperation.task_id == task.id)
        )
        task.status = (
            "manual_review" if operation is not None else ("rejected" if rejected else "failed")
        )
        task.result = None
        task.error_code = code
        task.error_detail = "任务执行异常，需要人工核对。" if operation else "任务执行未完成。"
        task.updated_at = now
        clear_lease(task)
        await append_event(
            session,
            task,
            "task.manual_review" if operation else "task.failed",
            {"error_code": code},
        )
