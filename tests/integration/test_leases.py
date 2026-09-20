"""独立事务真实争抢与过期 fencing；不依赖进程本地时钟。"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from aftercare.models import Event, Task


async def expire(database, task_id):
    async with database.sessions.begin() as session:
        await session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )


async def test_two_workers_claim_exactly_one_lease(app_database, create_task):
    from aftercare.runtime.leases import claim_next

    created = await create_task()
    claims = await asyncio.gather(*[claim_next(app_database, f"worker-{i}") for i in range(2)])
    leases = [lease for lease in claims if lease]
    assert len(leases) == 1 and leases[0].task_id == created.task_id
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert task.lease_epoch == 1 and task.status == "running"
        assert task.generation_started_at is not None
        assert task.last_event_seq == 2


async def test_expired_owner_cannot_renew_or_commit(app_database, create_task):
    from aftercare.runtime.leases import LeaseLost, claim_next, renew_lease, schedule_retry

    created = await create_task()
    old = await claim_next(app_database, "old")
    assert await renew_lease(app_database, old)
    await expire(app_database, created.task_id)
    assert not await renew_lease(app_database, old)
    with pytest.raises(LeaseLost):
        await schedule_retry(app_database, old, delay_seconds=1)
    async with app_database.sessions() as session:
        started = (await session.get(Task, created.task_id)).generation_started_at
    new = await claim_next(app_database, "new")
    assert new.epoch == old.epoch + 1
    with pytest.raises(LeaseLost):
        await schedule_retry(app_database, old, delay_seconds=0)
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert task.generation_started_at == started
        events = (
            await session.scalars(
                select(Event).where(Event.task_id == created.task_id).order_by(Event.seq)
            )
        ).all()
        assert [row.seq for row in events] == [1, 2, 3]
        assert events[-1].type == "task.recovered"


async def test_retry_clears_owner_and_respects_next_run(app_database, create_task):
    from aftercare.runtime.leases import claim_next, schedule_retry

    created = await create_task()
    lease = await claim_next(app_database, "worker")
    await schedule_retry(app_database, lease, delay_seconds=30)
    assert await claim_next(app_database, "other") is None
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert task.status == "queued"
        assert task.lease_owner is None and task.lease_expires_at is None
