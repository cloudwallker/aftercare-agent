"""Worker 先取得本地槽位，再领取任务；外部调用不持有 task 锁。"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from aftercare.models import Task


async def test_unconfigured_worker_does_not_claim(app_database, create_task):
    from aftercare.worker import Worker

    created = await create_task()
    with pytest.raises(ValueError, match="handler"):
        Worker(app_database, "worker", handlers={})
    async with app_database.sessions() as session:
        assert (await session.get(Task, created.task_id)).status == "queued"


async def test_worker_slots_heartbeat_and_transaction_boundary(app_database, create_task):
    from aftercare.runtime.leases import schedule_retry
    from aftercare.worker import Worker

    first, second = await create_task(), await create_task()
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def handler(lease):
        seen.append(lease.task_id)
        entered.set()
        await release.wait()
        await schedule_retry(app_database, lease, delay_seconds=30)

    worker = Worker(
        app_database,
        "worker",
        handlers={"investigate": handler},
        concurrency=1,
        heartbeat_seconds=0.02,
    )
    run1 = asyncio.create_task(worker.once())
    await asyncio.wait_for(entered.wait(), 3)
    run2 = asyncio.create_task(worker.once())
    try:
        async with app_database.sessions.begin() as session:
            task = await session.scalar(
                select(Task).where(Task.id == first.task_id).with_for_update(nowait=True)
            )
            assert task.status == "running"
            task.lease_expires_at = func.clock_timestamp() + timedelta(seconds=1)
            assert (await session.get(Task, second.task_id)).status == "queued"
        for _ in range(100):
            async with app_database.sessions() as session:
                task = await session.get(Task, first.task_id)
                now = await session.scalar(select(func.clock_timestamp()))
                if (task.lease_expires_at - now).total_seconds() > 10:
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("心跳未延长租约")
        assert seen == [first.task_id]
        release.set()
        assert await asyncio.wait_for(run1, 3)
        assert await asyncio.wait_for(run2, 3)
        assert seen == [first.task_id, second.task_id]
    finally:
        release.set()
        for task in (run1, run2):
            task.cancel()
        await asyncio.gather(run1, run2, return_exceptions=True)


async def test_worker_cancels_handler_after_lease_loss(app_database, create_task):
    from aftercare.worker import Worker

    created = await create_task()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(lease):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker = Worker(app_database, "old", handlers={"investigate": handler}, heartbeat_seconds=0.02)
    running = asyncio.create_task(worker.once())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with app_database.sessions.begin() as session:
            await session.execute(
                update(Task)
                .where(Task.id == created.task_id)
                .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        assert await asyncio.wait_for(running, 3)
        assert cancelled.is_set()
        async with app_database.sessions() as session:
            task = await session.get(Task, created.task_id)
            assert task.status == "running" and task.error_code is None
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_handler_return_without_transition_is_not_success(app_database, create_task):
    from aftercare.worker import Worker

    created = await create_task()

    async def incomplete(lease):
        pass

    worker = Worker(app_database, "worker", handlers={"investigate": incomplete})
    assert await worker.once()
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert task.status == "failed" and task.error_code == "HANDLER_INCOMPLETE"
        assert task.result is None and task.lease_owner is None


async def test_shutdown_cancels_own_handler_without_claiming_next(app_database, create_task):
    from aftercare.worker import Worker

    first, second = await create_task(), await create_task()
    entered, cancelled, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(lease):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker = Worker(
        app_database,
        "worker",
        handlers={"investigate": handler},
        concurrency=1,
        shutdown_seconds=0.05,
    )
    running = asyncio.create_task(worker.run(stop))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        stop.set()
        await asyncio.wait_for(running, 3)
        assert cancelled.is_set()
        async with app_database.sessions() as session:
            assert (await session.get(Task, first.task_id)).status == "running"
            assert (await session.get(Task, second.task_id)).status == "queued"
    finally:
        stop.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
