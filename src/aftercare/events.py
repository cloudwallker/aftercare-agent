"""每任务事件序号；调用方必须已在当前事务持有 task 行锁。"""

import asyncio
import json
import logging
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aftercare.models import Event, Task

EVENT_TYPES = frozenset(
    {
        "task.created",
        "task.claimed",
        "task.recovered",
        "step.started",
        "step.completed",
        "step.failed",
        "task.retry_scheduled",
        "approval.requested",
        "approval.approved",
        "approval.rejected",
        "approval.expired",
        "approval.invalidated",
        "task.reassessed",
        "refund.prepared",
        "refund.dispatching",
        "refund.reconciling",
        "refund.confirmed",
        "refund.declined",
        "task.completed",
        "task.failed",
        "task.manual_review",
    }
)


async def append_event(session: AsyncSession, task: Task, type: str, data: dict[str, Any]) -> Event:
    """不自行提交：状态、检查点和序号由同一短事务保护。新插入任务等价于持锁。"""
    started = time.monotonic()
    if type not in EVENT_TYPES:
        raise ValueError("未定义的事件类型")
    task.last_event_seq += 1
    event = Event(
        task_id=task.id,
        seq=task.last_event_seq,
        type=type,
        data={**data, "task_id": str(task.id), "generation": task.generation},
    )
    session.add(event)
    await session.flush()
    logging.getLogger(__name__).info(
        json.dumps(
            {
                "event": "event_staged",
                "event_type": type,
                "task_id": str(task.id),
                "generation": task.generation,
                "lease_epoch": task.lease_epoch,
                "step_id": data.get("step_id"),
                "operation_key": data.get("operation_key"),
                "event_seq": task.last_event_seq,
                "error_code": data.get("error_code"),
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }
        )
    )
    return event


TERMINAL = {"succeeded", "rejected", "failed", "manual_review"}


async def event_batch(database, actor, task_id, cursor):
    from aftercare.tasks import visible_task

    async with database.sessions() as session:
        task = await visible_task(session, actor, task_id)
        status, high = task.status, task.last_event_seq
        rows = (
            await session.scalars(
                select(Event)
                .where(Event.task_id == task_id, Event.seq > cursor, Event.seq <= high)
                .order_by(Event.seq)
                .limit(100)
            )
        ).all()
        return status, high, [{"seq": row.seq, "type": row.type, "data": row.data} for row in rows]


async def stream_events(database, actor, task_id, cursor, request, *, poll=1, heartbeat=15):
    loop = asyncio.get_running_loop()
    last_heartbeat = loop.time()
    while not await request.is_disconnected():
        try:
            status, high, rows = await event_batch(database, actor, task_id, cursor)
        except SQLAlchemyError:
            logging.getLogger(__name__).warning(
                "event_stream_closed task_id=%s error_code=DB_ERROR", task_id
            )
            return
        for row in rows:
            data = json.dumps(row["data"], ensure_ascii=False)
            yield f"id: {row['seq']}\nevent: {row['type']}\ndata: {data}\n\n"
            cursor = row["seq"]
        if status in TERMINAL and cursor >= high:
            return
        if loop.time() - last_heartbeat >= heartbeat:
            yield ": heartbeat\n\n"
            last_heartbeat = loop.time()
        if cursor < high:
            continue
        await asyncio.sleep(poll)
