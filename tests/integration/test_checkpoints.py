"""恢复预算、输出复用和 checkpoint/step/event 原子性。"""

from datetime import timedelta

import pytest
from sqlalchemy import event, func, select, update

from aftercare.models import Event, Step, Task


async def test_pending_attempt_budget_survives_takeover(app_database, create_task):
    from aftercare.runtime.leases import claim_next
    from aftercare.runtime.steps import BudgetExceeded, reserve_step

    created = await create_task()
    old = await claim_next(app_database, "old")
    first = await reserve_step(app_database, old, kind="tool", name="get_order", input={})
    async with app_database.sessions.begin() as session:
        await session.execute(
            update(Task)
            .where(Task.id == created.task_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    new = await claim_next(app_database, "new")
    second = await reserve_step(app_database, new, kind="tool", name="get_order", input={})
    third = await reserve_step(app_database, new, kind="tool", name="get_order", input={})
    assert first.id == second.id == third.id
    assert [first.attempt_count, second.attempt_count, third.attempt_count] == [1, 2, 3]
    with pytest.raises(BudgetExceeded):
        await reserve_step(app_database, new, kind="tool", name="get_order", input={})
    async with app_database.sessions() as session:
        assert (await session.get(Task, created.task_id)).tool_call_count == 1


async def test_completed_step_reused_and_stale_attempt_rejected(app_database, create_task):
    from aftercare.runtime.leases import claim_next
    from aftercare.runtime.steps import StepConflict, complete_step, reserve_step

    created = await create_task()
    lease = await claim_next(app_database, "worker")
    first = await reserve_step(app_database, lease, kind="model", name="next_action", input={})
    second = await reserve_step(app_database, lease, kind="model", name="next_action", input={})
    output = {"kind": "tool", "name": "get_order", "arguments": {}}
    with pytest.raises(StepConflict):
        await complete_step(app_database, lease, first, output)
    await complete_step(app_database, lease, second, output)
    replay = await reserve_step(
        app_database, lease, kind="model", name="next_action", input={}, step_no=1
    )
    assert replay.reused and replay.output == output
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert task.model_call_count == 2
        assert task.checkpoint["next_step_no"] == 2
        assert len(task.checkpoint["messages"]) == 1


async def test_event_failure_rolls_back_completion(app_database, create_task):
    from aftercare.runtime.leases import claim_next
    from aftercare.runtime.steps import complete_step, reserve_step

    created = await create_task()
    lease = await claim_next(app_database, "worker")
    attempt = await reserve_step(app_database, lease, kind="tool", name="get_order", input={})
    async with app_database.sessions() as session:
        before = (await session.get(Task, created.task_id)).last_event_seq

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO app.events"):
            raise RuntimeError("事件提交失败")

    event.listen(app_database.engine.sync_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            await complete_step(app_database, lease, attempt, {"order_version": 1})
    finally:
        event.remove(app_database.engine.sync_engine, "before_cursor_execute", fail)
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        step = await session.get(Step, attempt.id)
        assert step.status == "pending" and step.output is None
        assert task.checkpoint["next_step_no"] == 1 and not task.checkpoint["messages"]
        assert task.last_event_seq == before
        assert (
            max(
                (
                    await session.scalars(select(Event.seq).where(Event.task_id == created.task_id))
                ).all()
            )
            == before
        )


async def test_expired_lease_cannot_complete_step(app_database, create_task):
    from aftercare.runtime.leases import LeaseLost, claim_next
    from aftercare.runtime.steps import complete_step, reserve_step

    created = await create_task()
    lease = await claim_next(app_database, "worker")
    attempt = await reserve_step(app_database, lease, kind="tool", name="get_order", input={})
    async with app_database.sessions.begin() as session:
        await session.execute(
            update(Task)
            .where(Task.id == created.task_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    with pytest.raises(LeaseLost):
        await complete_step(app_database, lease, attempt, {"order_version": 1})


async def test_invalid_output_count_and_wall_clock_are_persistent(app_database, create_task):
    from aftercare.runtime.leases import claim_next
    from aftercare.runtime.steps import BudgetExceeded, record_invalid_output, reserve_step

    created = await create_task()
    lease = await claim_next(app_database, "worker")
    assert await record_invalid_output(app_database, lease)
    assert not await record_invalid_output(app_database, lease)
    async with app_database.sessions.begin() as session:
        task = await session.get(Task, created.task_id)
        assert task.checkpoint["invalid_output_count"] == 2
        task.generation_started_at = func.clock_timestamp() - timedelta(seconds=181)
    with pytest.raises(BudgetExceeded):
        await reserve_step(app_database, lease, kind="model", name="next_action", input={})


@pytest.mark.parametrize(
    "kind,name,maximum,counter",
    [("model", "next_action", 8, "model_call_count"), ("tool", "get_order", 6, "tool_call_count")],
)
async def test_generation_call_budget_is_enforced(
    app_database, create_task, kind, name, maximum, counter
):
    from aftercare.runtime.leases import claim_next
    from aftercare.runtime.steps import BudgetExceeded, complete_step, reserve_step

    created = await create_task()
    lease = await claim_next(app_database, "worker")
    for _ in range(maximum):
        attempt = await reserve_step(app_database, lease, kind=kind, name=name, input={})
        await complete_step(app_database, lease, attempt, {})
    with pytest.raises(BudgetExceeded):
        await reserve_step(app_database, lease, kind=kind, name=name, input={})
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert getattr(task, counter) == maximum
        assert task.checkpoint["next_step_no"] == maximum + 1


async def test_local_policy_counts_once_and_pending_choice_cannot_change(app_database, create_task):
    from aftercare.runtime.leases import claim_next
    from aftercare.runtime.steps import StepConflict, complete_step, reserve_step

    created = await create_task()
    lease = await claim_next(app_database, "worker")
    attempt = await reserve_step(
        app_database, lease, kind="tool", name="get_refund_policy", input={}
    )
    for _ in range(4):
        recovered = await reserve_step(
            app_database, lease, kind="tool", name="get_refund_policy", input={}
        )
        assert recovered.id == attempt.id and recovered.attempt_count == 0
    with pytest.raises(StepConflict):
        await reserve_step(app_database, lease, kind="tool", name="get_order", input={})
    await complete_step(app_database, lease, recovered, {"policy_version": "refund_v1"})
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert task.tool_call_count == 1
        assert task.checkpoint["evidence_by_tool"] == {"get_refund_policy": str(attempt.id)}


@pytest.mark.parametrize(
    "kind,name,limit", [("model", "next_action", 8192), ("tool", "get_order", 16384)]
)
async def test_oversized_output_cannot_partially_commit(
    app_database, create_task, kind, name, limit
):
    from aftercare.runtime.leases import claim_next
    from aftercare.runtime.steps import PayloadTooLarge, complete_step, reserve_step

    created = await create_task()
    lease = await claim_next(app_database, "worker")
    attempt = await reserve_step(app_database, lease, kind=kind, name=name, input={})
    with pytest.raises(PayloadTooLarge):
        await complete_step(app_database, lease, attempt, {"text": "a" * limit})
    async with app_database.sessions() as session:
        assert (await session.get(Step, attempt.id)).status == "pending"
        assert (await session.get(Task, created.task_id)).checkpoint["next_step_no"] == 1


async def test_step_timeout_uses_remaining_generation_time(app_database, create_task):
    from aftercare.runtime.leases import claim_next
    from aftercare.runtime.steps import reserve_step

    created = await create_task()
    lease = await claim_next(app_database, "worker")
    async with app_database.sessions.begin() as session:
        await session.execute(
            update(Task)
            .where(Task.id == created.task_id)
            .values(generation_started_at=func.clock_timestamp() - timedelta(seconds=179))
        )
    attempt = await reserve_step(app_database, lease, kind="model", name="next_action", input={})
    assert 0 < attempt.timeout_seconds <= 1


async def test_reservation_event_failure_does_not_charge_budget(app_database, create_task):
    from aftercare.runtime.leases import claim_next
    from aftercare.runtime.steps import reserve_step

    created = await create_task()
    lease = await claim_next(app_database, "worker")

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO app.events"):
            raise RuntimeError("步骤预扣事件保存失败")

    event.listen(app_database.engine.sync_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            await reserve_step(app_database, lease, kind="model", name="next_action", input={})
    finally:
        event.remove(app_database.engine.sync_engine, "before_cursor_execute", fail)
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert task.model_call_count == 0 and task.last_event_seq == 2
        assert not (await session.scalars(select(Step).where(Step.task_id == task.id))).all()


async def test_checkpoint_size_limit_rolls_back_step(app_database, create_task):
    from aftercare.runtime.leases import claim_next
    from aftercare.runtime.steps import PayloadTooLarge, complete_step, reserve_step

    created = await create_task()
    lease = await claim_next(app_database, "worker")
    attempt = await reserve_step(app_database, lease, kind="tool", name="get_order", input={})
    async with app_database.sessions.begin() as session:
        task = await session.get(Task, created.task_id)
        task.checkpoint = {**task.checkpoint, "messages": [{"output": "x" * 130000}]}
    with pytest.raises(PayloadTooLarge):
        await complete_step(app_database, lease, attempt, {"text": "y" * 2000})
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert task.checkpoint["next_step_no"] == 1 and len(task.checkpoint["messages"]) == 1
        assert (await session.get(Step, attempt.id)).status == "pending"
