"""商家事务账本：同键重放优先于订单状态检查。"""

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from aftercare.contracts import ConfirmedRefund, DeclinedRefund, canonical_hash
from aftercare.contracts import RefundRequest as RefundPayload
from aftercare.db import Database
from mock_merchant.models import Order, RefundRequest


class IdempotencyConflict(Exception):
    """已使用的 key 不能指向另一个冻结请求。"""


@dataclass(frozen=True)
class LedgerResult:
    body: dict[str, Any]
    http_status: int
    replayed: bool = False
    lose_response: bool = False


def rejection(order: Order | None, payload: RefundPayload) -> str | None:
    if order is None or order.customer_id != payload.customer_id:
        return "ORDER_NOT_FOUND"
    if order.refunded:
        return "ALREADY_REFUNDED"
    if order.order_version != payload.expected_order_version:
        return "ORDER_CHANGED"
    if (
        payload.policy_version != "refund_v1"
        or order.payment_status != "paid"
        or order.shipment_status != "delayed"
        or order.delay_days < 7
        or order.currency != "CNY"
        or payload.currency != order.currency
        or not 0 < order.paid_amount_cents <= 100000
        or payload.amount_cents != order.paid_amount_cents
    ):
        return "POLICY_DENIED"
    return None


async def refund(
    database: Database,
    key: str,
    payload: RefundPayload,
    *,
    lose_first_response: bool = False,
) -> LedgerResult:
    request = payload.model_dump(mode="json")
    fingerprint = canonical_hash(request)
    async with database.sessions.begin() as session:
        inserted = await session.scalar(
            insert(RefundRequest)
            .values(
                operation_key=key,
                request_hash=fingerprint,
                request=request,
                order_id=payload.order_id,
                status="processing",
            )
            .on_conflict_do_nothing(index_elements=[RefundRequest.operation_key])
            .returning(RefundRequest.operation_key)
        )
        # READ COMMITTED 的新语句读取冲突事务已经提交的终态。
        record = await session.get(RefundRequest, key)
        if record is None:
            raise RuntimeError("商家幂等账本缺失")
        if inserted is None:
            if record.request_hash != fingerprint:
                raise IdempotencyConflict
            if record.status not in {"confirmed", "declined"} or record.response is None:
                raise RuntimeError("商家账本没有终态")
            result = LedgerResult(record.response, record.response_http_status, replayed=True)
        else:
            order = await session.scalar(
                select(Order).where(Order.id == payload.order_id).with_for_update()
            )
            code = rejection(order, payload)
            if code:
                body = DeclinedRefund(
                    status="declined",
                    operation_key=key,
                    request_hash=fingerprint,
                    error_code=code,
                ).model_dump(mode="json")
                status = 404 if code == "ORDER_NOT_FOUND" else 409
                record.status = "declined"
            else:
                refund_id = uuid4()
                body = ConfirmedRefund(
                    status="confirmed",
                    operation_key=key,
                    request_hash=fingerprint,
                    order_id=payload.order_id,
                    amount_cents=payload.amount_cents,
                    currency="CNY",
                    refund_id=refund_id,
                ).model_dump(mode="json")
                status = 200
                order.refunded = True
                order.order_version += 1
                order.updated_at = func.clock_timestamp()
                record.status = "confirmed"
                record.refund_id = refund_id
                record.fault_consumed = lose_first_response
            record.response = body
            record.response_http_status = status
            record.completed_at = func.clock_timestamp()
            result = LedgerResult(body, status, lose_response=bool(record.fault_consumed))
    # 离开事务上下文完成提交，HTTP 层此时才能返回响应或注入响应丢失。
    return result


async def lookup(database: Database, key: str) -> dict[str, Any] | None:
    async with database.sessions() as session:
        record = await session.get(RefundRequest, key)
        if record is None:
            return None
        if record.status not in {"confirmed", "declined"} or record.response is None:
            raise RuntimeError("商家账本没有终态")
        return record.response
