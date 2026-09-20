"""模型和调用者不能覆盖身份、金额，摘要必须稳定且严格。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError


def test_task_input_rejects_customer_override():
    from aftercare.contracts import CreateTask

    with pytest.raises(ValidationError):
        CreateTask.model_validate({"order_id": "ORD-1", "message": "退款", "customer_id": "other"})


@pytest.mark.parametrize("message", ["", " " * 3, "字" * 2001])
def test_task_input_rejects_empty_or_excessive_message(message):
    from aftercare.contracts import CreateTask

    with pytest.raises(ValidationError):
        CreateTask(order_id="ORD-1", message=message)


def test_final_action_cannot_override_amount():
    from aftercare.contracts import FinalAction

    with pytest.raises(ValidationError):
        FinalAction.model_validate(
            {
                "kind": "final",
                "recommendation": "recommend_refund",
                "summary": "申请全额退款",
                "evidence_step_ids": [],
                "amount_cents": 1,
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "tool", "name": "execute_refund", "arguments": {}},
        {"kind": "tool", "name": "get_order", "arguments": {"order_id": "OTHER"}},
    ],
)
def test_tool_action_cannot_escape_read_only_scope(payload):
    from aftercare.contracts import ToolAction

    with pytest.raises(ValidationError):
        ToolAction.model_validate(payload)


def test_final_action_rejects_duplicate_evidence():
    from aftercare.contracts import FinalAction

    evidence = str(uuid4())
    with pytest.raises(ValidationError):
        FinalAction.model_validate(
            {
                "kind": "final",
                "recommendation": "no_action",
                "summary": "查询完成",
                "evidence_step_ids": [evidence] * 2,
            }
        )


@pytest.mark.parametrize("amount", [True, 1.5, "19900", 0, -1])
def test_refund_request_requires_positive_integer_cents(amount):
    from aftercare.contracts import RefundRequest

    with pytest.raises(ValidationError):
        RefundRequest.model_validate(
            {
                "order_id": "ORD-1",
                "customer_id": "customer_demo",
                "amount_cents": amount,
                "currency": "CNY",
                "expected_order_version": 1,
                "policy_version": "refund_v1",
                "authorization_id": str(uuid4()),
            }
        )


def test_canonical_hash_is_order_independent_and_rejects_nan():
    from aftercare.contracts import canonical_hash

    assert canonical_hash({"b": 2, "a": 1}) == (
        "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    )
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
    with pytest.raises(ValueError):
        canonical_hash({"amount": float("nan")})
