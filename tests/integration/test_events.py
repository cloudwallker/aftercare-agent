"""持久事件分页与事务提交顺序。TCP 心跳/断开另有端到端测试。"""

import asyncio

from sqlalchemy import select

from aftercare.events import append_event, event_batch
from aftercare.models import Task


async def test_terminal_history_larger_than_one_batch(app_database, app_actor, create_task):
    created = await create_task()
    async with app_database.sessions.begin() as session:
        task = await session.scalar(
            select(Task).where(Task.id == created.task_id).with_for_update()
        )
        for _ in range(105):
            await append_event(session, task, "step.started", {})
        task.status = "succeeded"
        await append_event(session, task, "task.completed", {})
    status, high, first = await event_batch(app_database, app_actor, created.task_id, 0)
    assert status == "succeeded" and high == 107 and len(first) == 100
    _, _, second = await event_batch(app_database, app_actor, created.task_id, 100)
    assert [e["seq"] for e in second] == list(range(101, 108))


async def test_interleaved_commit_cannot_skip_final_event(app_database, app_actor, create_task):
    created = await create_task()
    locked, release = asyncio.Event(), asyncio.Event()

    async def writer():
        async with app_database.sessions.begin() as session:
            task = await session.scalar(
                select(Task).where(Task.id == created.task_id).with_for_update()
            )
            await append_event(session, task, "step.started", {})
            locked.set()
            await release.wait()

    async def terminal():
        async with app_database.sessions.begin() as session:
            task = await session.scalar(
                select(Task).where(Task.id == created.task_id).with_for_update()
            )
            task.status = "succeeded"
            await append_event(session, task, "task.completed", {})

    first = asyncio.create_task(writer())
    await locked.wait()
    last = asyncio.create_task(terminal())
    status, high, rows = await event_batch(app_database, app_actor, created.task_id, 1)
    assert status == "queued" and high == 1 and rows == []
    release.set()
    await asyncio.gather(first, last)
    status, high, rows = await event_batch(app_database, app_actor, created.task_id, 1)
    assert status == "succeeded" and high == 3
    assert [r["seq"] for r in rows] == [2, 3]
