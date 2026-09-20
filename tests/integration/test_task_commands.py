"""命令重放、身份范围和状态/事件的原子提交。"""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, update

from aftercare.models import CommandRequest, Event, Task


async def test_twenty_creates_replay_original_response(client, app_database, app_actor):
    key = str(uuid4())
    payload = {"order_id": "NO-MERCHANT-LOOKUP", "message": "退款"}
    replies = await asyncio.gather(
        *[
            client.post("/v1/tasks", json=payload, headers={"Idempotency-Key": key})
            for _ in range(20)
        ]
    )
    assert {r.status_code for r in replies} == {201}
    assert len({r.json()["task_id"] for r in replies}) == 1
    assert sum(r.headers.get("Idempotency-Replayed") == "true" for r in replies) == 19
    task_id = UUID(replies[0].json()["task_id"])
    async with app_database.sessions.begin() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Task).where(Task.customer_id == app_actor.subject)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(Event).where(Event.task_id == task_id)
            )
            == 1
        )
        await session.execute(update(Task).where(Task.id == task_id).values(status="failed"))
    replay = await client.post("/v1/tasks", json=payload, headers={"Idempotency-Key": key})
    assert replay.status_code == 201 and replay.json() == replies[0].json()
    assert (await client.get(f"/v1/tasks/{task_id}")).json()["status"] == "failed"
    conflict = await client.post(
        "/v1/tasks", json={**payload, "message": "不同"}, headers={"Idempotency-Key": key}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_create_event_failure_rolls_back_command_and_task(app_database, app_actor):
    from aftercare.contracts import CreateTask
    from aftercare.tasks import create_task

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO app.events"):
            raise RuntimeError("测试事件保存失败")

    event.listen(app_database.engine.sync_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            await create_task(app_database, app_actor, CreateTask(order_id="O", message="退"), "k")
    finally:
        event.remove(app_database.engine.sync_engine, "before_cursor_execute", fail)
    async with app_database.sessions() as session:
        assert not (
            await session.scalars(select(Task).where(Task.customer_id == app_actor.subject))
        ).all()
        assert not (
            await session.scalars(
                select(CommandRequest).where(CommandRequest.actor_id == app_actor.subject)
            )
        ).all()


async def test_auth_input_and_task_visibility(client, reviewer, app_database, create_task):
    payload = {"order_id": "O", "message": "退"}
    assert (
        await reviewer.post("/v1/tasks", json=payload, headers={"Idempotency-Key": "k"})
    ).status_code == 403
    assert (await client.get("/v1/tasks", headers={"Authorization": "bad"})).status_code == 401
    for key in (None, "", "x" * 129, "bad\tkey"):
        response = await client.post(
            "/v1/tasks", json=payload, headers={} if key is None else {"Idempotency-Key": key}
        )
        assert response.status_code == 422 and "error" in response.json()
    invalid = await client.post(
        "/v1/tasks", json={**payload, "customer_id": "other"}, headers={"Idempotency-Key": "k"}
    )
    assert invalid.status_code == 422
    result = await create_task()
    detail = (await reviewer.get(f"/v1/tasks/{result.task_id}")).json()
    assert detail["id"] == str(result.task_id)
    assert "lease_owner" not in detail and "checkpoint" not in detail
    assert detail["approval"] is None and detail["operation"] is None
    assert (await client.get(f"/v1/tasks/{uuid4()}")).status_code == 404
    async with app_database.sessions.begin() as session:
        task = await session.get(Task, result.task_id)
        original = task.customer_id
        task.customer_id = "other"
    try:
        assert (await client.get(f"/v1/tasks/{result.task_id}")).status_code == 404
        assert (await reviewer.get(f"/v1/tasks/{result.task_id}")).status_code == 200
    finally:
        async with app_database.sessions.begin() as session:
            await session.execute(
                update(Task).where(Task.id == result.task_id).values(customer_id=original)
            )


async def test_task_cursor_pagination(client, create_task):
    ids = {str((await create_task()).task_id) for _ in range(3)}
    seen, cursor = set(), None
    for _ in range(3):
        params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
        response = await client.get("/v1/tasks", params=params)
        assert response.status_code == 200
        page = response.json()
        assert len(page["items"]) == 1
        seen.add(page["items"][0]["id"])
        cursor = page["next_cursor"]
    assert seen == ids and cursor is None
    assert (await client.get("/v1/tasks?cursor=garbage")).status_code == 422
    assert (await client.get("/v1/tasks?limit=101")).status_code == 422


async def test_step_pages_and_health(client, app_database, create_task):
    from aftercare.runtime.leases import claim_next
    from aftercare.runtime.steps import complete_step, reserve_step

    created = await create_task()
    lease = await claim_next(app_database, "worker")
    for name in ("get_order", "get_shipment"):
        attempt = await reserve_step(app_database, lease, kind="tool", name=name, input={})
        await complete_step(app_database, lease, attempt, {"order_version": 1})
    path = f"/v1/tasks/{created.task_id}/steps"
    first = (await client.get(path, params={"limit": 1})).json()
    second = (await client.get(path, params={"limit": 1, "cursor": first["next_cursor"]})).json()
    assert first["items"][0]["name"] == "get_order"
    assert second["items"][0]["name"] == "get_shipment"
    assert second["next_cursor"] is None
    assert (await client.get("/health/ready", headers={"Authorization": ""})).status_code == 200
    assert (await client.get("/health/live", headers={"Authorization": ""})).status_code == 200
