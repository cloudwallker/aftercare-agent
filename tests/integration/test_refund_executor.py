"""退款执行用真实数据库和商家事务检验未知结果、竞争及原子提交。"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from aftercare.approvals import decide_approval, expire_approvals
from aftercare.contracts import Actor, ApprovalDecision
from aftercare.models import RefundOperation, Task
from aftercare.refunds.executor import execute_refund, prepare_operation
from aftercare.refunds.merchant_client import MerchantError, RefundMerchantClient
from aftercare.runtime.leases import LeaseLost, claim_next
from mock_merchant.models import Order, RefundRequest


async def state(db, task_id):
    async with db.sessions() as session:
        return (
            await session.get(Task, task_id),
            await session.scalar(select(RefundOperation).where(RefundOperation.task_id == task_id)),
        )


async def ready(db, task_id):
    async with db.sessions.begin() as session:
        task = await session.get(Task, task_id)
        if task.status == "queued":
            task.next_run_at = func.clock_timestamp()
    return await claim_next(db, str(uuid4()))


async def ledger_count(db, order_id):
    async with db.sessions() as session:
        rows = (
            await session.scalars(
                select(RefundRequest).where(
                    RefundRequest.order_id == order_id, RefundRequest.status == "confirmed"
                )
            )
        ).all()
        return rows


async def test_happy_path(approved_refund, app_database, merchant_client, merchant_database):
    approval, order = await approved_refund()
    lease = await ready(app_database, approval.task_id)
    await execute_refund(app_database, lease, RefundMerchantClient(merchant_client))
    task, operation = await state(app_database, approval.task_id)
    rows = await ledger_count(merchant_database, order.id)
    assert task.status == "succeeded" and task.result["outcome"] == "refunded"
    assert operation.status == "confirmed" and len(rows) == 1
    assert operation.merchant_refund_id == rows[0].refund_id
    assert (operation.dispatch_count, operation.reconcile_count) == (1, 1)
    assert task.lease_owner is None


async def test_lost_response_reconciles_without_second_post(
    approved_refund, app_database, merchant_database, merchant_app_factory
):
    approval, order = await approved_refund()
    app = merchant_app_factory(testing=True, fault_mode="commit_then_503_once")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://merchant",
        headers={"Authorization": "Bearer test-merchant-token"},
    ) as client:
        merchant = RefundMerchantClient(client)
        await execute_refund(app_database, await ready(app_database, approval.task_id), merchant)
        task, operation = await state(app_database, approval.task_id)
        assert task.status == "queued" and operation.status == "unknown"
        await execute_refund(app_database, await ready(app_database, approval.task_id), merchant)
    task, operation = await state(app_database, approval.task_id)
    assert (
        task.status == "succeeded"
        and operation.dispatch_count == 1
        and operation.reconcile_count == 2
    )
    assert len(await ledger_count(merchant_database, order.id)) == 1


async def test_version_change_is_definite_decline(
    approved_refund, app_database, merchant_client, merchant_database
):
    approval, order = await approved_refund()
    async with merchant_database.sessions.begin() as session:
        row = await session.get(Order, order.id)
        row.order_version += 1
    await execute_refund(
        app_database,
        await ready(app_database, approval.task_id),
        RefundMerchantClient(merchant_client),
    )
    task, operation = await state(app_database, approval.task_id)
    assert task.status == "rejected" and task.error_code == "ORDER_CHANGED"
    assert operation.status == "declined" and not await ledger_count(merchant_database, order.id)


async def test_two_tasks_reserve_same_order(
    approved_refund, pending_approval, app_actor, app_database, merchant_client, merchant_database
):
    first, order = await approved_refund()
    second = await pending_approval(order.id)
    await decide_approval(
        app_database,
        Actor(f"reviewer-{app_actor.subject}", "reviewer"),
        second.id,
        ApprovalDecision(generation=1, payload_hash=second.payload_hash, decision="approve"),
        str(uuid4()),
    )
    leases = [await claim_next(app_database, str(uuid4())) for _ in range(2)]
    merchant = RefundMerchantClient(merchant_client)
    await asyncio.gather(*(execute_refund(app_database, lease, merchant) for lease in leases))
    states = [await state(app_database, a.task_id) for a in (first, second)]
    assert sorted(t.status for t, op in states) == ["rejected", "succeeded"]
    assert [t.error_code for t, op in states].count("ORDER_REFUND_RESERVED") == 1
    assert sum(op is not None for t, op in states) == 1
    assert len(await ledger_count(merchant_database, order.id)) == 1


@pytest.mark.parametrize("mode", ["unavailable", "missing", "mismatch"])
async def test_bounded_uncertainty(approved_refund, app_database, mode):
    approval, _ = await approved_refund()

    class Uncertain:
        posts = 0

        async def lookup_refund(self, key, timeout):
            if mode == "unavailable":
                raise MerchantError("UNAVAILABLE", True)
            return {} if mode == "mismatch" else None

        async def post_refund(self, key, payload, timeout):
            self.posts += 1
            raise MerchantError("LOST_RESPONSE", True)

    merchant = Uncertain()
    for _ in range(8):
        await execute_refund(app_database, await ready(app_database, approval.task_id), merchant)
    task, operation = await state(app_database, approval.task_id)
    assert task.status == "manual_review" and task.result is None
    assert operation.reconcile_count == 8
    assert operation.dispatch_count == merchant.posts == (3 if mode == "missing" else 0)
    assert operation.status == ("unknown" if merchant.posts else "prepared")


async def test_final_post_lost_response_still_gets_reconciled(
    approved_refund, app_database, merchant_client, merchant_database
):
    approval, order = await approved_refund()

    class Third(RefundMerchantClient):
        posts = 0

        async def post_refund(self, key, payload, timeout):
            self.posts += 1
            if self.posts == 3:
                await super().post_refund(key, payload, timeout)
            raise MerchantError("LOST_RESPONSE", True)

    merchant = Third(merchant_client)
    for _ in range(4):
        await execute_refund(app_database, await ready(app_database, approval.task_id), merchant)
    task, operation = await state(app_database, approval.task_id)
    assert (
        task.status == "succeeded"
        and operation.dispatch_count == 3
        and operation.reconcile_count == 4
    )
    assert len(await ledger_count(merchant_database, order.id)) == 1


async def test_last_get_missing_does_not_dispatch(approved_refund, app_database):
    approval, _ = await approved_refund()
    lease = await ready(app_database, approval.task_id)
    await prepare_operation(app_database, lease)
    async with app_database.sessions.begin() as session:
        op = await session.scalar(
            select(RefundOperation).where(RefundOperation.task_id == approval.task_id)
        )
        op.reconcile_count = 7

    class Last:
        async def lookup_refund(self, key, timeout):
            return None

        async def post_refund(self, *args):
            raise AssertionError("最后一次 GET missing 后不得再 POST")

    await execute_refund(app_database, lease, Last())
    task, op = await state(app_database, approval.task_id)
    assert task.status == "manual_review" and op.dispatch_count == 0


@pytest.mark.parametrize("frozen", [False, True])
async def test_expiry_before_and_after_freeze(
    approved_refund, app_database, merchant_client, frozen
):
    approval, _ = await approved_refund(seconds=0.8)
    lease = await ready(app_database, approval.task_id)
    if frozen:
        await prepare_operation(app_database, lease)
    await asyncio.sleep(0.9)
    # 扫描不能抢占 running，无论是否已经冻结。
    await expire_approvals(app_database)
    assert (await state(app_database, approval.task_id))[0].status == "running"
    await execute_refund(app_database, lease, RefundMerchantClient(merchant_client))
    task, op = await state(app_database, approval.task_id)
    assert task.status == ("succeeded" if frozen else "rejected")
    assert (op is not None) == frozen
    if not frozen:
        assert task.error_code == "AUTHORIZATION_EXPIRED"


async def test_result_transaction_rolls_back_then_get_recovers(
    approved_refund, app_database, merchant_client, merchant_database, monkeypatch
):
    from aftercare.refunds import executor

    approval, order = await approved_refund()
    lease = await ready(app_database, approval.task_id)
    original = executor.append_event

    async def fail(session, task, kind, data):
        if kind == "task.completed":
            await session.flush()
            raise RuntimeError("提交终态事件失败")
        return await original(session, task, kind, data)

    monkeypatch.setattr(executor, "append_event", fail)
    with pytest.raises(RuntimeError):
        await execute_refund(app_database, lease, RefundMerchantClient(merchant_client))
    task, op = await state(app_database, approval.task_id)
    assert task.status == "running" and op.status == "unknown" and op.merchant_result is None
    monkeypatch.setattr(executor, "append_event", original)
    await execute_refund(app_database, lease, RefundMerchantClient(merchant_client))
    task, op = await state(app_database, approval.task_id)
    assert task.status == "succeeded" and op.dispatch_count == 1
    assert len(await ledger_count(merchant_database, order.id)) == 1


@pytest.mark.parametrize("window", ["before_refund_http", "after_refund_http_before_commit"])
async def test_stale_worker_cannot_commit_after_external_success(
    approved_refund, app_database, merchant_client, merchant_database, window
):
    approval, order = await approved_refund()
    old = await ready(app_database, approval.task_id)

    async def takeover(name, lease):
        if name != window:
            return
        async with app_database.sessions.begin() as session:
            task = await session.get(Task, approval.task_id)
            task.lease_expires_at = func.clock_timestamp() - timedelta(seconds=1)
        new = await claim_next(app_database, "replacement")
        await execute_refund(app_database, new, RefundMerchantClient(merchant_client))

    with pytest.raises(LeaseLost):
        await execute_refund(
            app_database, old, RefundMerchantClient(merchant_client), hook=takeover
        )
    task, op = await state(app_database, approval.task_id)
    assert task.status == "succeeded" and op.dispatch_count == (
        2 if window == "before_refund_http" else 1
    )
    assert len(await ledger_count(merchant_database, order.id)) == 1


async def test_full_api_investigation_approval_refund(
    client, reviewer, app_database, app_actor, seed_order, merchant_client, merchant_database
):
    from uuid import UUID

    from aftercare.agent.mock_provider import MockProvider
    from aftercare.agent.runner import run_investigation
    from aftercare.refunds.merchant_client import MerchantClient
    from aftercare.worker import Worker

    order = await seed_order(customer_id=app_actor.subject)
    response = await client.post(
        "/v1/tasks",
        json={"order_id": order.id, "message": "申请退款"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201
    task_id = UUID(response.json()["task_id"])

    async def investigate(lease):
        await run_investigation(
            app_database, lease, MockProvider(), MerchantClient(merchant_client)
        )

    async def refund(lease):
        await execute_refund(app_database, lease, RefundMerchantClient(merchant_client))

    worker = Worker(
        app_database, "full", handlers={"investigate": investigate, "execute_refund": refund}
    )
    assert await worker.once()
    view = (await client.get(f"/v1/tasks/{task_id}")).json()
    approval = view["approval"]
    assert view["status"] == "waiting_approval"
    decision = {"generation": 1, "payload_hash": approval["payload_hash"], "decision": "approve"}
    key = str(uuid4())
    responses = await asyncio.gather(
        *[
            reviewer.post(
                f"/v1/approvals/{approval['id']}/decision",
                json=decision,
                headers={"Idempotency-Key": key},
            )
            for _ in range(20)
        ]
    )
    assert {r.status_code for r in responses} == {200}
    assert await worker.once()
    view = (await client.get(f"/v1/tasks/{task_id}")).json()
    assert view["status"] == "succeeded" and view["operation"]["status"] == "confirmed"
    assert len(await ledger_count(merchant_database, order.id)) == 1


async def test_scanner_expires_unfrozen_but_preserves_queued_operation(
    approved_refund, app_database, merchant_client
):
    first, _ = await approved_refund(seconds=0.8)
    second, _ = await approved_refund(seconds=0.8)
    lease = await ready(app_database, first.task_id)

    class Offline:
        async def lookup_refund(self, *args):
            raise MerchantError("UNAVAILABLE", True)

    await execute_refund(app_database, lease, Offline())
    await asyncio.sleep(0.9)
    await expire_approvals(app_database)
    assert (await state(app_database, first.task_id))[0].status == "queued"
    task, op = await state(app_database, second.task_id)
    assert task.status == "rejected" and task.error_code == "AUTHORIZATION_EXPIRED" and op is None
    await execute_refund(
        app_database,
        await ready(app_database, first.task_id),
        RefundMerchantClient(merchant_client),
    )
    assert (await state(app_database, first.task_id))[0].status == "succeeded"


async def test_worker_exception_after_freeze_is_manual_review(approved_refund, app_database):
    from aftercare.worker import Worker

    approval, _ = await approved_refund()

    async def crash(lease):
        await prepare_operation(app_database, lease)
        raise RuntimeError("模拟冻结后的程序错误")

    worker = Worker(app_database, "crash", handlers={"investigate": crash, "execute_refund": crash})
    assert await worker.once()
    task, op = await state(app_database, approval.task_id)
    assert task.status == "manual_review" and op.status == "prepared"


async def test_prepare_failure_rolls_back_reservation(approved_refund, app_database, monkeypatch):
    from aftercare.refunds import executor

    approval, _ = await approved_refund()
    lease = await ready(app_database, approval.task_id)

    async def fail(*args):
        raise RuntimeError("模拟冻结事件失败")

    monkeypatch.setattr(executor, "append_event", fail)
    with pytest.raises(RuntimeError):
        await prepare_operation(app_database, lease)
    task, op = await state(app_database, approval.task_id)
    assert task.status == "running" and op is None


async def test_expired_reconciliation_deadline_makes_no_http(approved_refund, app_database):
    from aftercare.contracts import RefundRequest as Payload
    from aftercare.contracts import canonical_hash

    approval, order = await approved_refund()
    lease = await ready(app_database, approval.task_id)
    request = Payload(
        order_id=order.id,
        customer_id=order.customer_id,
        amount_cents=19900,
        currency="CNY",
        expected_order_version=1,
        policy_version="refund_v1",
        authorization_id=approval.id,
    ).model_dump(mode="json")
    async with app_database.sessions.begin() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        session.add(
            RefundOperation(
                id=uuid4(),
                task_id=approval.task_id,
                approval_id=approval.id,
                order_id=order.id,
                generation=1,
                operation_key=f"refund:{approval.task_id}:1",
                request=request,
                request_hash=canonical_hash(request),
                status="unknown",
                reconcile_deadline=now - timedelta(seconds=1),
            )
        )
    await execute_refund(app_database, lease, None)
    task, op = await state(app_database, approval.task_id)
    assert task.status == "manual_review" and op.status == "unknown"
    assert op.reconcile_count == op.dispatch_count == 0


async def test_inspection_is_read_only(approved_refund, app_database):
    from aftercare.refunds.inspection import inspect_refund

    approval, _ = await approved_refund()
    lease = await ready(app_database, approval.task_id)
    await prepare_operation(app_database, lease)
    before_task, before_op = await state(app_database, approval.task_id)

    class ReadOnly:
        async def lookup_refund(self, key, timeout):
            assert key == before_op.operation_key
            return None

    result = await inspect_refund(app_database, ReadOnly(), approval.task_id)
    task, op = await state(app_database, approval.task_id)
    assert result["operation_key"] == before_op.operation_key and result["merchant_result"] is None
    assert task.status == before_task.status and task.last_event_seq == before_task.last_event_seq
    assert op.dispatch_count == op.reconcile_count == 0 and op.status == "prepared"
