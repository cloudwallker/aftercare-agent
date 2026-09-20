"""种子数据重复执行不能覆盖已运行的退款历史。"""

import asyncio
from uuid import uuid4

from sqlalchemy import select, update

from mock_merchant.models import Order, RefundRequest


async def test_seed_preserves_history_and_existing_orders(merchant_database, new_order_id):
    from mock_merchant.seed import seed_orders

    prefix = f"SEED-{uuid4()}-"
    ids = [new_order_id(f"{prefix}ORD-{number}") for number in range(1001, 1007)]
    await seed_orders(merchant_database, prefix=prefix)
    async with merchant_database.sessions.begin() as session:
        await session.execute(
            update(Order)
            .where(Order.id == ids[0])
            .values(payment_status="cancelled", order_version=2)
        )
        before = (
            await session.scalars(select(RefundRequest).where(RefundRequest.order_id == ids[3]))
        ).one()
        original = (before.operation_key, before.refund_id, before.response)
    await seed_orders(merchant_database, prefix=prefix)
    async with merchant_database.sessions() as session:
        orders = (await session.scalars(select(Order).where(Order.id.in_(ids)))).all()
        rows = (
            await session.scalars(select(RefundRequest).where(RefundRequest.order_id.in_(ids)))
        ).all()
        assert len(orders) == 6 and len(rows) == 1
        changed = await session.get(Order, ids[0])
        assert changed.payment_status == "cancelled" and changed.order_version == 2
        assert (rows[0].operation_key, rows[0].refund_id, rows[0].response) == original
        refunded = await session.get(Order, ids[3])
        assert refunded.refunded and refunded.order_version == 1
        assert rows[0].response["amount_cents"] == 19900
        assert rows[0].status == "confirmed"


async def test_concurrent_seed_creates_one_matching_ledger(merchant_database, new_order_id):
    from mock_merchant.seed import seed_orders

    prefix = f"SEED-{uuid4()}-"
    ids = [new_order_id(f"{prefix}ORD-{number}") for number in range(1001, 1007)]
    await asyncio.gather(*[seed_orders(merchant_database, prefix=prefix) for _ in range(4)])
    async with merchant_database.sessions() as session:
        orders = (await session.scalars(select(Order).where(Order.id.in_(ids)))).all()
        rows = (
            await session.scalars(select(RefundRequest).where(RefundRequest.order_id.in_(ids)))
        ).all()
        assert len(orders) == 6 and len(rows) == 1
        assert rows[0].order_id == ids[3]
        assert str(rows[0].refund_id) == rows[0].response["refund_id"]


async def test_seed_does_not_reset_a_refund_completed_via_http(
    merchant_database, new_order_id, merchant_client
):
    from mock_merchant.seed import seed_orders

    prefix = f"SEED-{uuid4()}-"
    ids = [new_order_id(f"{prefix}ORD-{number}") for number in range(1001, 1007)]
    await seed_orders(merchant_database, prefix=prefix)
    key = str(uuid4())
    response = await merchant_client.post(
        "/refunds",
        headers={"Idempotency-Key": key},
        json={
            "order_id": ids[0],
            "customer_id": "customer_demo",
            "amount_cents": 19900,
            "currency": "CNY",
            "expected_order_version": 1,
            "policy_version": "refund_v1",
            "authorization_id": str(uuid4()),
        },
    )
    assert response.status_code == 200
    await seed_orders(merchant_database, prefix=prefix)
    async with merchant_database.sessions() as session:
        order = await session.get(Order, ids[0])
        assert order.refunded and order.order_version == 2
        row = await session.get(RefundRequest, key)
        assert row.response == response.json()
