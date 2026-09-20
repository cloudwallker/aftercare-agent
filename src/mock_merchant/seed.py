"""插入缺失的模拟数据；已有订单与退款历史始终保留。"""

import argparse
import asyncio
import sys
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from aftercare.config import load_settings
from aftercare.contracts import RefundRequest as RefundPayload
from aftercare.contracts import canonical_hash
from aftercare.db import Database, create_database
from mock_merchant.models import Order, RefundRequest


async def seed_orders(database: Database, *, prefix: str = "") -> int:
    """prefix 可用于演示生成独立订单；整个批次与初始退款账本原子提交。"""
    if len(prefix) > 64 or any(not (32 <= ord(char) <= 126) for char in prefix):
        raise ValueError("prefix 必须为至多 64 个可打印 ASCII 字符")
    inserted = 0
    async with database.sessions.begin() as session:
        for number in range(1001, 1007):
            data = {
                "id": f"{prefix}ORD-{number}",
                "customer_id": "customer_other" if number == 1005 else "customer_demo",
                "paid_amount_cents": {1002: 9900, 1003: 29900, 1006: 120000}.get(number, 19900),
                "currency": "CNY",
                "payment_status": "paid",
                "shipment_status": "delivered" if number == 1002 else "delayed",
                "delay_days": {1002: 0, 1003: 2}.get(number, 8),
                "refunded": number == 1004,
                "order_version": 1,
            }
            order_id = await session.scalar(
                insert(Order)
                .values(**data)
                .on_conflict_do_nothing(index_elements=[Order.id])
                .returning(Order.id)
            )
            if order_id is None:
                continue
            inserted += 1
            if not data["refunded"]:
                continue
            operation_key = f"seed-refund:{order_id}"
            refund_id = uuid5(NAMESPACE_URL, operation_key)
            payload = RefundPayload(
                order_id=order_id,
                customer_id=data["customer_id"],
                amount_cents=data["paid_amount_cents"],
                currency="CNY",
                expected_order_version=1,
                policy_version="refund_v1",
                authorization_id=uuid5(NAMESPACE_URL, f"seed-authorization:{order_id}"),
            ).model_dump(mode="json")
            request_hash = canonical_hash(payload)
            response = {
                "status": "confirmed",
                "operation_key": operation_key,
                "request_hash": request_hash,
                "order_id": order_id,
                "amount_cents": data["paid_amount_cents"],
                "currency": "CNY",
                "refund_id": str(refund_id),
            }
            session.add(
                RefundRequest(
                    operation_key=operation_key,
                    request_hash=request_hash,
                    request=payload,
                    order_id=order_id,
                    status="confirmed",
                    refund_id=refund_id,
                    response=response,
                    response_http_status=200,
                    completed_at=func.clock_timestamp(),
                )
            )
    return inserted


async def run(prefix: str) -> None:
    settings = load_settings()
    if settings.role != "seed":
        raise ValueError("seed 命令要求 APP_ROLE=seed")
    database = create_database(settings.database_url)
    try:
        count = await seed_orders(database, prefix=prefix)
        print(f"新增 {count} 个模拟订单，已有订单与退款历史保持不变。")
    finally:
        await database.engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="幂等初始化模拟商家订单")
    parser.add_argument("--prefix", default="", help="可选独立演示订单前缀")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run(args.prefix))


if __name__ == "__main__":
    main()
