"""通过真实 PostgreSQL 验证商家并发幂等与事务事实。"""

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, select, update

from aftercare.contracts import canonical_hash
from mock_merchant.models import Order, RefundRequest

pytestmark = pytest.mark.asyncio


def request_body(order, **overrides):
    return {
        "order_id": order.id,
        "customer_id": order.customer_id,
        "amount_cents": order.paid_amount_cents,
        "currency": order.currency,
        "expected_order_version": order.order_version,
        "policy_version": "refund_v1",
        "authorization_id": str(uuid4()),
        **overrides,
    }


async def facts(database, order_id):
    async with database.sessions() as session:
        order = await session.get(Order, order_id)
        rows = (
            await session.scalars(select(RefundRequest).where(RefundRequest.order_id == order_id))
        ).all()
        return order, rows


async def test_same_key_replays_refund(merchant_client, seed_order, merchant_database):
    order = await seed_order()
    payload, key = request_body(order), str(uuid4())
    responses = [
        await merchant_client.post("/refunds", json=payload, headers={"Idempotency-Key": key})
        for _ in range(2)
    ]
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert responses[1].headers["Idempotency-Replayed"] == "true"
    body = responses[0].json()
    assert body["status"] == "confirmed"
    assert body["request_hash"] == canonical_hash(payload)
    assert body["order_id"] == order.id
    assert body["amount_cents"] == order.paid_amount_cents
    saved, rows = await facts(merchant_database, order.id)
    assert saved.refunded and saved.order_version == order.order_version + 1
    assert len(rows) == 1 and rows[0].status == "confirmed"
    assert rows[0].response == body
    assert (await merchant_client.get(f"/refunds/by-key/{key}")).json() == body


async def test_ascii_key_with_slash_can_be_queried(merchant_client, seed_order):
    order = await seed_order()
    key = f"refund/{uuid4()}"
    posted = await merchant_client.post(
        "/refunds", json=request_body(order), headers={"Idempotency-Key": key}
    )
    assert posted.status_code == 200
    found = await merchant_client.get(f"/refunds/by-key/{key}")
    assert found.status_code == 200
    assert found.json() == posted.json()


@pytest.mark.parametrize("changed_field", ["authorization_id", "amount_cents", "order_id"])
async def test_same_key_changed_parameters_conflicts(merchant_client, seed_order, changed_field):
    order = await seed_order()
    payload, key = request_body(order), str(uuid4())
    first = await merchant_client.post("/refunds", json=payload, headers={"Idempotency-Key": key})
    payload[changed_field] = 1 if changed_field == "amount_cents" else str(uuid4())
    changed = await merchant_client.post("/refunds", json=payload, headers={"Idempotency-Key": key})
    assert first.status_code == 200
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert (await merchant_client.get(f"/refunds/by-key/{key}")).json() == first.json()


@pytest.mark.parametrize("same_key", [True, False])
async def test_twenty_concurrent_requests_refund_once(
    merchant_client, seed_order, merchant_database, same_key
):
    order = await seed_order()
    payload, key = request_body(order), str(uuid4())
    responses = await asyncio.gather(
        *[
            merchant_client.post(
                "/refunds",
                json=payload,
                headers={"Idempotency-Key": key if same_key else str(uuid4())},
            )
            for _ in range(20)
        ]
    )
    assert sum(r.status_code == 200 for r in responses) == (20 if same_key else 1)
    assert all(r.status_code in {200, 409} for r in responses)
    successful = [r.json() for r in responses if r.status_code == 200]
    assert len({r["refund_id"] for r in successful}) == 1
    saved, rows = await facts(merchant_database, order.id)
    assert saved.refunded and saved.order_version == order.order_version + 1
    assert sum(row.status == "confirmed" for row in rows) == 1
    assert len(rows) == (1 if same_key else 20)
    assert all(row.status in {"confirmed", "declined"} for row in rows)
    assert all(row.response and row.completed_at for row in rows)


@pytest.mark.parametrize(
    ("overrides", "payload_changes", "code", "status"),
    [
        ({}, {"customer_id": "another-customer"}, "ORDER_NOT_FOUND", 404),
        ({"order_version": 2}, {"expected_order_version": 1}, "ORDER_CHANGED", 409),
        ({"refunded": True}, {}, "ALREADY_REFUNDED", 409),
        ({"payment_status": "unpaid"}, {}, "POLICY_DENIED", 409),
        ({"shipment_status": "delivered"}, {}, "POLICY_DENIED", 409),
        ({"delay_days": 6}, {}, "POLICY_DENIED", 409),
        ({"currency": "USD"}, {}, "POLICY_DENIED", 409),
        ({"paid_amount_cents": 100001}, {}, "POLICY_DENIED", 409),
        ({}, {"amount_cents": 1}, "POLICY_DENIED", 409),
        ({}, {"currency": "USD"}, "POLICY_DENIED", 409),
        ({}, {"policy_version": "refund_v2"}, "POLICY_DENIED", 409),
    ],
)
async def test_declined_results_are_durable(
    merchant_client, seed_order, merchant_database, overrides, payload_changes, code, status
):
    order = await seed_order(**overrides)
    payload, key = request_body(order, **payload_changes), str(uuid4())
    first = await merchant_client.post("/refunds", json=payload, headers={"Idempotency-Key": key})
    replay = await merchant_client.post("/refunds", json=payload, headers={"Idempotency-Key": key})
    assert first.status_code == replay.status_code == status
    assert first.json() == replay.json()
    assert first.json()["status"] == "declined"
    assert first.json()["error_code"] == code
    lookup = await merchant_client.get(f"/refunds/by-key/{key}")
    assert lookup.status_code == 200 and lookup.json() == first.json()
    saved, rows = await facts(merchant_database, order.id)
    assert saved.refunded == order.refunded and saved.order_version == order.order_version
    assert len(rows) == 1 and rows[0].status == "declined"


async def test_missing_order_decline_is_saved(merchant_client, seed_order, new_order_id):
    order = await seed_order()
    key = str(uuid4())
    response = await merchant_client.post(
        "/refunds",
        json=request_body(order, order_id=new_order_id()),
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "ORDER_NOT_FOUND"
    assert (await merchant_client.get(f"/refunds/by-key/{key}")).json() == response.json()


async def test_lost_response_survives_application_restart(
    merchant_app_factory, seed_order, merchant_database
):
    order = await seed_order()
    payload, key = request_body(order), str(uuid4())
    for attempt in range(2):
        app = merchant_app_factory(testing=True, fault_mode="commit_then_503_once")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://merchant",
            headers={"Authorization": "Bearer test-merchant-token"},
        ) as client:
            response = await client.post("/refunds", json=payload, headers={"Idempotency-Key": key})
            assert response.status_code == (503 if attempt == 0 else 200)
            found = await client.get(f"/refunds/by-key/{key}")
            assert found.status_code == 200 and found.json()["status"] == "confirmed"
            if attempt:
                assert response.json() == found.json()
    saved, rows = await facts(merchant_database, order.id)
    assert saved.order_version == order.order_version + 1
    assert len(rows) == 1 and rows[0].fault_consumed


async def test_order_visibility_and_service_auth(merchant_client, seed_order):
    order = await seed_order()
    for path in (f"/orders/{order.id}", f"/orders/{order.id}/shipment"):
        visible = await merchant_client.get(path, params={"customer_id": order.customer_id})
        assert visible.status_code == 200
        assert visible.json()["order_version"] == order.order_version
        if path.endswith("/shipment"):
            assert visible.json() == {
                "order_id": order.id,
                "shipment_status": order.shipment_status,
                "delay_days": order.delay_days,
                "order_version": order.order_version,
            }
        else:
            assert visible.json() == {
                "id": order.id,
                "customer_id": order.customer_id,
                "paid_amount_cents": order.paid_amount_cents,
                "currency": order.currency,
                "payment_status": order.payment_status,
                "refunded": order.refunded,
                "order_version": order.order_version,
            }
        assert (await merchant_client.get(path, params={"customer_id": "other"})).status_code == 404
        assert (
            await merchant_client.get(
                path,
                headers={"Authorization": "Bearer wrong"},
                params={"customer_id": order.customer_id},
            )
        ).status_code == 401
    assert (await merchant_client.get(f"/refunds/by-key/{uuid4()}")).status_code == 404
    assert (
        await merchant_client.get("/health/live", headers={"Authorization": ""})
    ).status_code == 200
    assert (
        await merchant_client.get("/health/ready", headers={"Authorization": ""})
    ).status_code == 200


@pytest.mark.parametrize("key", [None, "", "x" * 129, "bad\tkey"])
async def test_invalid_key_has_no_effect(merchant_client, seed_order, merchant_database, key):
    order = await seed_order()
    response = await merchant_client.post(
        "/refunds",
        json=request_body(order),
        headers={} if key is None else {"Idempotency-Key": key},
    )
    assert response.status_code == 422
    saved, rows = await facts(merchant_database, order.id)
    assert not saved.refunded and not rows


async def test_ledger_save_failure_rolls_back_order_and_key(
    merchant_app_factory, seed_order, merchant_database
):
    order = await seed_order()
    payload, key = request_body(order), str(uuid4())

    def fail_ledger_update(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE merchant.refund_requests"):
            raise RuntimeError("模拟账本保存失败")

    engine = merchant_database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", fail_ledger_update)
    try:
        app = merchant_app_factory()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://merchant",
            headers={"Authorization": "Bearer test-merchant-token"},
        ) as client:
            response = await client.post("/refunds", json=payload, headers={"Idempotency-Key": key})
            assert response.status_code == 500
    finally:
        event.remove(engine, "before_cursor_execute", fail_ledger_update)
    saved, rows = await facts(merchant_database, order.id)
    assert not saved.refunded and saved.order_version == order.order_version
    assert not rows


async def test_declined_replay_ignores_later_order_changes(
    merchant_client, seed_order, merchant_database
):
    order = await seed_order(payment_status="unpaid")
    payload, key = request_body(order), str(uuid4())
    first = await merchant_client.post("/refunds", json=payload, headers={"Idempotency-Key": key})
    assert first.status_code == 409 and first.json()["error_code"] == "POLICY_DENIED"
    async with merchant_database.sessions.begin() as session:
        await session.execute(
            update(Order)
            .where(Order.id == order.id)
            .values(payment_status="paid", order_version=order.order_version + 1)
        )
    replay = await merchant_client.post("/refunds", json=payload, headers={"Idempotency-Key": key})
    assert replay.status_code == 409 and replay.json() == first.json()
    saved, rows = await facts(merchant_database, order.id)
    assert not saved.refunded and len(rows) == 1
