"""真实 PostgreSQL 与商家 ASGI：调查不执行退款，恢复不更换已提交动作。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from aftercare.agent.mock_provider import MockProvider
from aftercare.agent.provider import ProviderError
from aftercare.agent.runner import run_investigation
from aftercare.contracts import FinalAction, ToolAction, canonical_hash
from aftercare.models import Approval, RefundOperation, Step, Task
from aftercare.refunds.merchant_client import MerchantClient
from aftercare.runtime.leases import claim_next
from aftercare.runtime.steps import complete_step, reserve_step
from aftercare.worker import Worker


@pytest.fixture
def investigation(app_database, app_actor, create_task, seed_order, merchant_client):
    calls = []

    async def record(request):
        calls.append(request.method)
        assert request.method == "GET", "调查阶段不能执行退款"

    merchant_client.event_hooks["request"].append(record)

    async def run(provider=None, overrides=None, message="申请退款"):
        order = await seed_order(customer_id=app_actor.subject, **(overrides or {}))
        created = await create_task(order_id=order.id, message=message)

        async def handler(lease):
            await run_investigation(
                app_database, lease, provider or MockProvider(), MerchantClient(merchant_client)
            )

        worker = Worker(app_database, "investigation", handlers={"investigate": handler})
        assert await worker.once()
        async with app_database.sessions() as session:
            task = await session.get(Task, created.task_id)
            approval = await session.scalar(select(Approval).where(Approval.task_id == task.id))
            assert (
                await session.scalar(
                    select(RefundOperation).where(RefundOperation.task_id == task.id)
                )
                is None
            )
            return task, approval, calls

    return run


async def test_approval_from_snapshots(investigation, client):
    task, approval, calls = await investigation()
    assert task.status == "waiting_approval" and task.lease_owner is None
    assert (task.model_call_count, task.tool_call_count) == (4, 3)
    assert approval.payload["amount_cents"] == 19900
    assert approval.payload["order_version"] == 1
    assert approval.payload_hash == canonical_hash(approval.payload)
    assert len(approval.payload["evidence_step_ids"]) == 3
    assert 86395 < (approval.expires_at - task.updated_at).total_seconds() <= 86400
    assert calls == ["GET", "GET"]
    view = (await client.get(f"/v1/tasks/{task.id}")).json()
    assert view["approval"]["payload_hash"] == approval.payload_hash


@pytest.mark.parametrize(
    "overrides",
    [
        {"delay_days": 2},
        {"refunded": True},
        {"paid_amount_cents": 100001},
        {"payment_status": "unpaid"},
        {"currency": "USD"},
    ],
)
async def test_model_cannot_bypass_policy(investigation, overrides):
    task, approval, _ = await investigation(overrides=overrides)
    assert (task.status, task.error_code) == ("rejected", "POLICY_DENIED")
    assert approval is None and task.lease_owner is None


async def test_no_action_still_queries_three_tools(investigation):
    task, approval, calls = await investigation(message="只查询物流状态")
    assert task.status == "succeeded" and task.result["outcome"] == "no_action"
    assert task.tool_call_count == 3 and len(calls) == 2 and approval is None


@pytest.mark.parametrize("bad", ["json", "tool", "evidence"])
async def test_invalid_output_has_one_correction(investigation, bad):
    class Invalid:
        async def next_action(self, messages, specs):
            if bad == "json":
                return "not-json"
            if bad == "tool":
                return {"kind": "tool", "name": "execute_refund", "arguments": {}}
            return FinalAction(
                recommendation="recommend_refund",
                summary="伪证据",
                evidence_step_ids=[uuid4(), uuid4(), uuid4()],
            )

    task, approval, calls = await investigation(Invalid())
    assert (task.status, task.error_code) == ("failed", "INVALID_MODEL_OUTPUT")
    assert task.model_call_count == 2 and task.checkpoint["invalid_output_count"] == 2
    assert approval is None and calls == []


async def test_correction_can_succeed(investigation):
    class Corrected(MockProvider):
        async def next_action(self, messages, specs):
            if len(messages) == 1:
                return "bad-json"
            return await super().next_action(messages, specs)

    task, approval, _ = await investigation(Corrected())
    assert task.status == "waiting_approval" and approval
    assert task.model_call_count == 5 and task.checkpoint["invalid_output_count"] == 1


async def test_infinite_tools_hit_budget(investigation):
    class Loop:
        async def next_action(self, messages, specs):
            return ToolAction(name="get_refund_policy", arguments={})

    task, approval, calls = await investigation(Loop())
    assert (task.status, task.error_code) == ("failed", "TOOL_BUDGET_EXCEEDED")
    assert task.tool_call_count == 6 and approval is None and calls == []


@pytest.mark.parametrize("pending", [False, True])
async def test_recovery_uses_saved_tool_choice(
    app_database, app_actor, create_task, seed_order, merchant_client, pending
):
    order = await seed_order(customer_id=app_actor.subject)
    created = await create_task(order_id=order.id)
    old = await claim_next(app_database, "old")
    attempt = await reserve_step(app_database, old, kind="model", name="next_action", input={})
    await complete_step(
        app_database,
        old,
        attempt,
        ToolAction(name="get_order", arguments={}).model_dump(mode="json"),
    )
    if pending:
        await reserve_step(app_database, old, kind="tool", name="get_order", input={})
    async with app_database.sessions.begin() as session:
        await session.execute(
            update(Task)
            .where(Task.id == created.task_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )

    class Observe(MockProvider):
        async def next_action(self, messages, specs):
            assert any(m.get("name") == "get_order" and m.get("kind") == "tool" for m in messages)
            return await super().next_action(messages, specs)

    lease = await claim_next(app_database, "new")
    await run_investigation(app_database, lease, Observe(), MerchantClient(merchant_client))
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        steps = (
            await session.scalars(
                select(Step).where(Step.task_id == task.id, Step.name == "get_order")
            )
        ).all()
        assert task.status == "waiting_approval" and task.model_call_count == 4
        assert len(steps) == 1 and steps[0].attempt_count == (2 if pending else 1)


async def test_retry_budget_survives_new_worker(app_database, create_task):
    created = await create_task()

    class Unavailable:
        async def next_action(self, messages, specs):
            raise ProviderError()

    for number in range(3):
        lease = await claim_next(app_database, f"worker-{number}")
        await run_investigation(app_database, lease, Unavailable(), None)
        async with app_database.sessions.begin() as session:
            task = await session.get(Task, created.task_id)
            if number < 2:
                assert task.status == "queued"
                assert (task.next_run_at - task.updated_at).total_seconds() == (
                    1 if number == 0 else 3
                )
                task.next_run_at = func.clock_timestamp()
            else:
                assert task.status == "failed" and task.model_call_count == 3
                step = await session.scalar(select(Step).where(Step.task_id == task.id))
                assert step.attempt_count == 3


async def test_approval_event_failure_rolls_back_and_resumes(
    app_database, app_actor, create_task, seed_order, merchant_client, monkeypatch
):
    from aftercare.agent import runner

    order = await seed_order(customer_id=app_actor.subject)
    created = await create_task(order_id=order.id)
    lease = await claim_next(app_database, "worker")
    real = runner.append_event

    async def fail(session, task, kind, data):
        if kind == "approval.requested":
            await session.flush()
            raise RuntimeError("模拟审批事件失败")
        return await real(session, task, kind, data)

    monkeypatch.setattr(runner, "append_event", fail)
    with pytest.raises(RuntimeError):
        await run_investigation(
            app_database, lease, MockProvider(), MerchantClient(merchant_client)
        )
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert task.status == "running" and task.lease_owner == "worker"
        assert await session.scalar(select(Approval).where(Approval.task_id == task.id)) is None
    monkeypatch.setattr(runner, "append_event", real)

    class NoCalls:
        async def next_action(self, messages, specs):
            raise AssertionError("已提交最终建议不应重新调用模型")

    await run_investigation(app_database, lease, NoCalls(), None)
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert task.status == "waiting_approval" and task.model_call_count == 4


@pytest.mark.parametrize("recover", [False, True])
async def test_snapshot_version_feedback(investigation, recover):
    class Versions(MockProvider):
        async def next_action(self, messages, specs):
            return await super().next_action(messages, specs)

    # 修改实际 HTTP 查询响应以模拟订单在两个只读请求之间发生变更。
    # 保留客户端验证和真实调查链路，版本来自商家响应而非模型。
    original = MerchantClient.read
    count = 0

    async def changed(self, name, *args):
        nonlocal count
        data = await original(self, name, *args)
        if name == "get_shipment":
            count += 1
            if not recover or count == 1:
                data["order_version"] += 1
        return data

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MerchantClient, "read", changed)
        task, approval, _ = await investigation(Versions())
    if recover:
        assert task.status == "waiting_approval" and approval
    else:
        assert (task.status, task.error_code) == ("failed", "SNAPSHOT_UNSTABLE")
        assert approval is None


@pytest.mark.parametrize("boundary", ["invalid", "tool"])
async def test_committed_checkpoint_survives_interruption(
    app_database, app_actor, create_task, seed_order, merchant_client, monkeypatch, boundary
):
    from aftercare.agent import runner

    order = await seed_order(customer_id=app_actor.subject)
    created = await create_task(order_id=order.id)
    lease = await claim_next(app_database, "worker")
    real = runner.complete_step

    async def interrupted(database, lease, attempt, output, **kwargs):
        await real(database, lease, attempt, output, **kwargs)
        if boundary == "invalid" or attempt.name == "get_order":
            raise RuntimeError("模拟提交后进程中断")

    class Bad:
        async def next_action(self, messages, specs):
            return "invalid"

    monkeypatch.setattr(runner, "complete_step", interrupted)
    with pytest.raises(RuntimeError):
        await run_investigation(
            app_database,
            lease,
            Bad() if boundary == "invalid" else MockProvider(),
            MerchantClient(merchant_client),
        )
    monkeypatch.setattr(runner, "complete_step", real)
    await run_investigation(
        app_database,
        lease,
        Bad() if boundary == "invalid" else MockProvider(),
        MerchantClient(merchant_client),
    )
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        if boundary == "invalid":
            assert task.error_code == "INVALID_MODEL_OUTPUT" and task.model_call_count == 2
            assert task.checkpoint["invalid_output_count"] == 2
        else:
            assert task.status == "waiting_approval" and task.model_call_count == 4
            steps = (
                await session.scalars(
                    select(Step).where(Step.task_id == task.id, Step.name == "get_order")
                )
            ).all()
            assert len(steps) == 1 and steps[0].attempt_count == 1


async def test_cross_task_evidence_is_rejected(investigation):
    _, approval, _ = await investigation()

    class Stolen:
        async def next_action(self, messages, specs):
            return FinalAction(
                recommendation="recommend_refund",
                summary="引用其他工单",
                evidence_step_ids=approval.payload["evidence_step_ids"],
            )

    task, approval, _ = await investigation(Stolen())
    assert task.error_code == "INVALID_MODEL_OUTPUT" and approval is None


async def test_other_customers_order_is_invisible(
    app_database, create_task, seed_order, merchant_client
):
    order = await seed_order(customer_id="another-customer")
    created = await create_task(order_id=order.id)
    lease = await claim_next(app_database, "worker")
    await run_investigation(app_database, lease, MockProvider(), MerchantClient(merchant_client))
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert task.error_code == "ORDER_NOT_FOUND" and task.status == "rejected"
        assert task.tool_call_count == 1


async def test_tool_transient_error_retries_same_step(
    app_database, app_actor, create_task, seed_order, merchant_client
):
    from aftercare.refunds.merchant_client import MerchantError

    order = await seed_order(customer_id=app_actor.subject)
    created = await create_task(order_id=order.id)

    class Flaky(MerchantClient):
        failed = False

        async def read(self, *args):
            if not self.failed:
                self.failed = True
                raise MerchantError("MERCHANT_UNAVAILABLE", True)
            return await super().read(*args)

    merchant = Flaky(merchant_client)
    lease = await claim_next(app_database, "first")
    await run_investigation(app_database, lease, MockProvider(), merchant)
    async with app_database.sessions.begin() as session:
        task = await session.get(Task, created.task_id)
        assert task.status == "queued" and task.tool_call_count == 1
        task.next_run_at = func.clock_timestamp()
    lease = await claim_next(app_database, "second")
    await run_investigation(app_database, lease, MockProvider(), merchant)
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        step = await session.scalar(
            select(Step).where(Step.task_id == task.id, Step.name == "get_order")
        )
        assert task.status == "waiting_approval"
        assert task.tool_call_count == 3 and task.model_call_count == 4 and step.attempt_count == 2


async def test_wall_clock_budget_prevents_external_call(app_database, create_task):
    created = await create_task()
    lease = await claim_next(app_database, "worker")
    async with app_database.sessions.begin() as session:
        task = await session.get(Task, created.task_id)
        task.generation_started_at = func.clock_timestamp() - timedelta(seconds=181)
    await run_investigation(app_database, lease, None, None)
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert task.error_code == "INVESTIGATION_TIMEOUT" and task.model_call_count == 0


async def test_provider_hard_timeout_schedules_bounded_retry(
    app_database, create_task, monkeypatch
):
    import asyncio
    from dataclasses import replace

    from aftercare.agent import runner

    created = await create_task()
    lease = await claim_next(app_database, "worker")
    original = runner.reserve_step

    async def short_timeout(*args, **kwargs):
        return replace(await original(*args, **kwargs), timeout_seconds=0.01)

    class Hanging:
        async def next_action(self, messages, specs):
            await asyncio.Event().wait()

    monkeypatch.setattr(runner, "reserve_step", short_timeout)
    await run_investigation(app_database, lease, Hanging(), None)
    async with app_database.sessions() as session:
        task = await session.get(Task, created.task_id)
        assert task.status == "queued" and task.model_call_count == 1
        assert task.lease_owner is None


@pytest.mark.parametrize("mode", ["valid", "invalid"])
async def test_http_provider_uses_same_runtime(investigation, mode):
    import json

    import httpx

    from aftercare.agent.http_provider import HttpProvider

    def handle(request):
        messages = json.loads(request.content)["messages"]
        outputs = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        names = ["get_order", "get_shipment", "get_refund_policy"]
        if mode == "invalid":
            message, reason = {"content": "not-json"}, "stop"
        elif len(outputs) < 3:
            message = {
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {"name": names[len(outputs)], "arguments": "{}"},
                    }
                ]
            }
            reason = "tool_calls"
        else:
            message = {
                "content": json.dumps(
                    {
                        "kind": "final",
                        "recommendation": "recommend_refund",
                        "summary": "查询完成",
                        "evidence_step_ids": [o["step_id"] for o in outputs],
                    }
                )
            }
            reason = "stop"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message, "finish_reason": reason}],
                "usage": {"total_tokens": 10},
            },
        )

    async with httpx.AsyncClient(
        base_url="http://fixture/v1/", transport=httpx.MockTransport(handle)
    ) as client:
        provider = HttpProvider(client, "fixture-model")
        task, approval, _ = await investigation(provider)
        assert provider.usage_complete and provider.token_count == provider.request_count * 10
    if mode == "valid":
        assert task.status == "waiting_approval" and approval
    else:
        assert task.error_code == "INVALID_MODEL_OUTPUT" and task.model_call_count == 2
