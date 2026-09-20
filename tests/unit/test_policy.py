"""规则边界必须由程序判断，不能由模型覆盖。"""

import pytest
from pydantic import ValidationError

from aftercare.refunds.policy import OrderSnapshot, ShipmentSnapshot, evaluate_refund

ORDER = dict(
    id="O",
    customer_id="C",
    paid_amount_cents=19900,
    currency="CNY",
    payment_status="paid",
    refunded=False,
    order_version=1,
)
SHIPMENT = dict(order_id="O", shipment_status="delayed", delay_days=7, order_version=1)


@pytest.mark.parametrize(
    "order,shipment,code",
    [
        ({}, {}, "ELIGIBLE"),
        ({"paid_amount_cents": 100000}, {}, "ELIGIBLE"),
        ({"paid_amount_cents": 100001}, {}, "POLICY_DENIED"),
        ({"paid_amount_cents": 0}, {}, "POLICY_DENIED"),
        ({"currency": "USD"}, {}, "POLICY_DENIED"),
        ({"payment_status": "unpaid"}, {}, "POLICY_DENIED"),
        ({"refunded": True}, {}, "POLICY_DENIED"),
        ({}, {"delay_days": 6}, "POLICY_DENIED"),
        ({}, {"shipment_status": "delivered"}, "POLICY_DENIED"),
        ({"customer_id": "other"}, {}, "ORDER_NOT_FOUND"),
        ({}, {"order_version": 2}, "INCONSISTENT_SNAPSHOT"),
        ({}, {"order_id": "other"}, "INCONSISTENT_SNAPSHOT"),
    ],
)
def test_rule_boundaries(order, shipment, code):
    result = evaluate_refund(
        OrderSnapshot(**(ORDER | order)), ShipmentSnapshot(**(SHIPMENT | shipment)), "C"
    )
    assert result.code == code
    assert result.eligible == (code == "ELIGIBLE")


@pytest.mark.parametrize("value", [True, 1.5, "19900"])
def test_amount_is_strict_integer(value):
    with pytest.raises(ValidationError):
        OrderSnapshot(**(ORDER | {"paid_amount_cents": value}))
