"""模型不可越权、覆盖执行参数或一次发出多个动作。"""

from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from aftercare.agent.provider import parse_action
from aftercare.refunds.merchant_client import MerchantClient, MerchantError
from aftercare.runtime.steps import PayloadTooLarge


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        [],
        {"kind": "tool", "name": "execute_refund", "arguments": {}},
        {"kind": "tool", "name": "get_order", "arguments": {"order_id": "other"}},
        {
            "kind": "final",
            "recommendation": "recommend_refund",
            "summary": "退款",
            "evidence_step_ids": [],
            "amount_cents": 1,
        },
        {
            "kind": "final",
            "recommendation": "recommend_refund",
            "summary": "x" * 501,
            "evidence_step_ids": [],
        },
    ],
)
def test_invalid_actions(value):
    with pytest.raises(ValidationError):
        parse_action(value)


def test_duplicate_evidence():
    uid = str(uuid4())
    with pytest.raises(ValidationError):
        parse_action(
            dict(
                kind="final",
                recommendation="no_action",
                summary="查询",
                evidence_step_ids=[uid, uid],
            )
        )


@pytest.mark.parametrize(
    "status,body,code,retryable",
    [
        (404, {}, "ORDER_NOT_FOUND", False),
        (401, {"secret": "never expose"}, "MERCHANT_PROTOCOL_ERROR", False),
        (503, {}, "MERCHANT_UNAVAILABLE", True),
        (429, {}, "MERCHANT_UNAVAILABLE", True),
        (200, {"secret": "never expose"}, "MERCHANT_PROTOCOL_ERROR", False),
    ],
)
async def test_merchant_errors_are_sanitized(status, body, code, retryable):
    async with httpx.AsyncClient(
        base_url="http://merchant",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)),
    ) as client:
        with pytest.raises(MerchantError) as caught:
            await MerchantClient(client).read("get_order", "O", "C", 5)
        assert str(caught.value) == code
        assert caught.value.retryable is retryable


async def test_merchant_output_bounded():
    async with httpx.AsyncClient(
        base_url="http://merchant",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b" " * 16385)),
    ) as client:
        with pytest.raises(PayloadTooLarge):
            await MerchantClient(client).read("get_order", "O", "C", 5)
