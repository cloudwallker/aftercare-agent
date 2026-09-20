"""真实 PostgreSQL 的审批竞争、重放和重新调查。"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from aftercare.approvals import expire_approvals
from aftercare.models import Approval, CommandRequest, RefundOperation, Task


def data(approval, decision="approve"):
    return {
        "generation": approval.generation,
        "payload_hash": approval.payload_hash,
        "decision": decision,
    }


async def decide(reviewer, approval, decision="approve", key=None):
    return await reviewer.post(
        f"/v1/approvals/{approval.id}/decision",
        json=data(approval, decision),
        headers={"Idempotency-Key": key or str(uuid4())},
    )


async def test_twenty_duplicate_and_terminal_replay(pending_approval, reviewer, app_database):
    approval = await pending_approval()
    key = str(uuid4())
    responses = await asyncio.gather(*[decide(reviewer, approval, key=key) for _ in range(20)])
    assert {r.status_code for r in responses} == {200}
    assert all(r.json() == responses[0].json() for r in responses)
    async with app_database.sessions.begin() as session:
        task = await session.get(Task, approval.task_id)
        assert task.status == "queued" and task.phase == "execute_refund"
        assert (
            await session.scalar(select(RefundOperation).where(RefundOperation.task_id == task.id))
            is None
        )
        task.status = "succeeded"
    replay = await decide(reviewer, approval, key=key)
    assert replay.status_code == 200 and replay.json() == responses[0].json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert (await decide(reviewer, approval, "reject", key)).status_code == 409


@pytest.mark.parametrize("opposite", [False, True])
async def test_distinct_keys_one_winner(pending_approval, reviewer, opposite):
    approval = await pending_approval()
    responses = await asyncio.gather(
        *[
            decide(reviewer, approval, "reject" if opposite and n % 2 else "approve")
            for n in range(20)
        ]
    )
    assert [r.status_code for r in responses].count(200) == 1
    assert [r.status_code for r in responses].count(409) == 19


async def test_approve_reassess_race(pending_approval, reviewer, client):
    approval = await pending_approval()
    responses = await asyncio.gather(
        decide(reviewer, approval),
        client.post(
            f"/v1/tasks/{approval.task_id}/reassess",
            json={"expected_generation": 1},
            headers={"Idempotency-Key": str(uuid4())},
        ),
    )
    assert sum(r.status_code == 409 for r in responses) == 1
    assert sum(r.status_code in {200, 202} for r in responses) == 1


async def test_reassess_resets_budget_and_old_approval(
    pending_approval, reviewer, client, app_database
):
    approval = await pending_approval()
    async with app_database.sessions.begin() as session:
        task = await session.get(Task, approval.task_id)
        task.generation_started_at = func.clock_timestamp() - timedelta(seconds=1000)
        task.model_call_count, task.tool_call_count = 8, 6
        task.checkpoint = {**task.checkpoint, "invalid_output_count": 1}
        epoch = task.lease_epoch
    key, path = str(uuid4()), f"/v1/tasks/{approval.task_id}/reassess"
    first = await client.post(
        path, json={"expected_generation": 1}, headers={"Idempotency-Key": key}
    )
    assert first.status_code == 202
    replay = await client.post(
        path, json={"expected_generation": 1}, headers={"Idempotency-Key": key}
    )
    assert replay.json() == first.json() and replay.headers["Idempotency-Replayed"] == "true"
    async with app_database.sessions() as session:
        task = await session.get(Task, approval.task_id)
        assert (task.generation, task.lease_epoch) == (2, epoch + 1)
        assert (
            task.generation_started_at is None
            and task.model_call_count == task.tool_call_count == 0
        )
        assert task.checkpoint == {
            "schema_version": 1,
            "generation": 2,
            "messages": [],
            "next_step_no": 1,
            "evidence_by_tool": {},
            "invalid_output_count": 0,
        }
    assert (await decide(reviewer, approval)).status_code == 409
    history = (await client.get(f"/v1/tasks/{approval.task_id}/approvals")).json()
    assert history["items"][0]["status"] == "invalidated"


async def test_maximum_generation_and_roles(pending_approval, client, reviewer):
    approval = await pending_approval(generation=3)
    path = f"/v1/tasks/{approval.task_id}/reassess"
    assert (
        await client.post(
            path, json={"expected_generation": 3}, headers={"Idempotency-Key": str(uuid4())}
        )
    ).status_code == 409
    assert (
        await reviewer.post(
            path, json={"expected_generation": 3}, headers={"Idempotency-Key": str(uuid4())}
        )
    ).status_code == 403
    assert (await decide(client, approval)).status_code == 403
    bad = data(approval) | {"payload_hash": "0" * 64}
    assert (
        await reviewer.post(
            f"/v1/approvals/{approval.id}/decision",
            json=bad,
            headers={"Idempotency-Key": str(uuid4())},
        )
    ).status_code == 409


async def test_expired_approval(pending_approval, reviewer, app_database):
    approval = await pending_approval(seconds=-1)
    assert (await decide(reviewer, approval)).status_code == 409
    assert await expire_approvals(app_database) >= 1
    async with app_database.sessions() as session:
        task = await session.get(Task, approval.task_id)
        assert (task.status, task.error_code) == ("rejected", "AUTHORIZATION_EXPIRED")
        assert (await session.get(Approval, approval.id)).status == "expired"


async def test_rejection_creates_no_operation(pending_approval, reviewer, app_database):
    approval = await pending_approval()
    assert (await decide(reviewer, approval, "reject")).status_code == 200
    async with app_database.sessions() as session:
        assert (await session.get(Task, approval.task_id)).error_code == "APPROVAL_REJECTED"
        assert (
            await session.scalar(
                select(RefundOperation).where(RefundOperation.task_id == approval.task_id)
            )
            is None
        )


async def test_event_failure_rolls_back_command(pending_approval, app_database, monkeypatch):
    from aftercare import approvals
    from aftercare.contracts import Actor, ApprovalDecision

    approval = await pending_approval()
    key = str(uuid4())

    async def fail(*args):
        raise RuntimeError("模拟事件提交失败")

    monkeypatch.setattr(approvals, "append_event", fail)
    with pytest.raises(RuntimeError):
        await approvals.decide_approval(
            app_database,
            Actor("reviewer", "reviewer"),
            approval.id,
            ApprovalDecision(**data(approval)),
            key,
        )
    async with app_database.sessions() as session:
        assert (await session.get(Approval, approval.id)).status == "pending"
        assert (await session.get(Task, approval.task_id)).status == "waiting_approval"
        assert (
            await session.scalar(
                select(CommandRequest).where(CommandRequest.idempotency_key == key)
            )
            is None
        )
