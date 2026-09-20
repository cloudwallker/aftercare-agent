"""唯一退款规则；金额和版本仅从严格校验的快照派生。"""

from typing import Annotated

from pydantic import Field, StrictBool, StrictInt

from aftercare.contracts import Contract, PositiveInt

POLICY = {
    "policy_version": "refund_v1",
    "minimum_delay_days": 7,
    "maximum_amount_cents": 100000,
    "currency": "CNY",
    "approval_required": True,
}


class OrderSnapshot(Contract):
    id: str
    customer_id: str
    paid_amount_cents: StrictInt
    currency: str
    payment_status: str
    refunded: StrictBool
    order_version: PositiveInt


class ShipmentSnapshot(Contract):
    order_id: str
    shipment_status: str
    delay_days: Annotated[StrictInt, Field(ge=0)]
    order_version: PositiveInt


class PolicyVerdict(Contract):
    eligible: bool
    code: str
    summary: str


def evaluate_refund(
    order: OrderSnapshot, shipment: ShipmentSnapshot, customer_id: str
) -> PolicyVerdict:
    if order.customer_id != customer_id:
        return PolicyVerdict(eligible=False, code="ORDER_NOT_FOUND", summary="未找到订单。")
    if order.id != shipment.order_id or order.order_version != shipment.order_version:
        return PolicyVerdict(
            eligible=False, code="INCONSISTENT_SNAPSHOT", summary="订单快照版本不一致，请重新查询。"
        )
    checks = [
        (order.payment_status == "paid", "订单尚未支付。"),
        (not order.refunded, "订单已经退款。"),
        (
            shipment.shipment_status == "delayed" and shipment.delay_days >= 7,
            "物流延误未达到七天。",
        ),
        (order.currency == "CNY", "订单币种不符合退款规则。"),
        (0 < order.paid_amount_cents <= 100000, "订单金额不在退款规则范围内。"),
    ]
    for accepted, summary in checks:
        if not accepted:
            return PolicyVerdict(eligible=False, code="POLICY_DENIED", summary=summary)
    return PolicyVerdict(
        eligible=True, code="ELIGIBLE", summary="订单符合全额退款规则，需人工审批。"
    )
