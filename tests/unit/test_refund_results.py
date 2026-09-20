"""确定结果必须匹配冻结请求，不能把错误 body 当作退款凭证。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from aftercare.refunds.executor import validate_result
from aftercare.refunds.merchant_client import MerchantError


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_key", "other"),
        ("request_hash", "b" * 64),
        ("order_id", "other"),
        ("amount_cents", 1),
        ("amount_cents", True),
        ("currency", "USD"),
        ("refund_id", "not-uuid"),
        ("status", "unknown"),
        ("extra", "field"),
    ],
)
def test_wrong_fingerprint_or_receipt_rejected(field, value):
    op = SimpleNamespace(
        operation_key="key",
        request_hash="a" * 64,
        order_id="order",
        request={"amount_cents": 19900, "currency": "CNY"},
    )
    result = {
        "status": "confirmed",
        "operation_key": "key",
        "request_hash": "a" * 64,
        "order_id": "order",
        "amount_cents": 19900,
        "currency": "CNY",
        "refund_id": str(uuid4()),
    }
    assert validate_result(op, result) == result
    with pytest.raises(MerchantError):
        validate_result(op, result | {field: value})
