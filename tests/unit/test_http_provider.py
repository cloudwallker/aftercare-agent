"""兼容接口消息序列、单调用约束及无内部重试。"""

import json
from uuid import uuid4

import httpx
import pytest

from aftercare.agent.http_provider import HttpProvider, wire_messages
from aftercare.agent.provider import InvalidModelOutput, ProviderError
from aftercare.agent.tools import TOOL_SPECS


def context():
    return [
        {
            "system_rules": "只读查询",
            "message": "申请退款",
            "order_id": "O",
            "customer_id": "C",
            "generation": 1,
        }
    ]


async def test_tool_history_survives_provider_recreation():
    mid, tid = str(uuid4()), str(uuid4())
    history = context() + [
        {
            "step_id": mid,
            "kind": "model",
            "name": "next_action",
            "output": {"kind": "tool", "name": "get_order", "arguments": {}},
        },
        {"step_id": tid, "kind": "tool", "name": "get_order", "output": {"id": "O"}},
    ]
    messages = wire_messages(history)
    assert messages[2]["role"] == "assistant"
    call = messages[2]["tool_calls"][0]
    assert messages[3]["role"] == "tool" and messages[3]["tool_call_id"] == call["id"]
    assert json.loads(messages[3]["content"])["step_id"] == tid

    def handle(request):
        body = json.loads(request.content)
        assert body["parallel_tool_calls"] is False and body["messages"] == messages
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-x",
                                    "type": "function",
                                    "function": {"name": "get_shipment", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="http://model/v1/", transport=httpx.MockTransport(handle)
    ) as client:
        action = await HttpProvider(client, "fixture-model").next_action(history, TOOL_SPECS)
        assert action.name == "get_shipment"


@pytest.mark.parametrize("status,retryable", [(429, True), (503, True), (401, False), (400, False)])
async def test_http_errors_are_sanitized_without_retry(status, retryable):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="secret response")

    async with httpx.AsyncClient(
        base_url="http://model/", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(ProviderError) as error:
            await HttpProvider(client, "fixture-model").next_action(context(), TOOL_SPECS)
        assert error.value.retryable is retryable and "secret" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "message,reason",
    [
        (
            {
                "tool_calls": [
                    {"type": "function", "function": {"name": "get_order", "arguments": "{}"}}
                ]
                * 2
            },
            "tool_calls",
        ),
        ({"content": "not-json"}, "stop"),
        (
            {
                "tool_calls": [
                    {"type": "function", "function": {"name": "execute_refund", "arguments": "{}"}}
                ]
            },
            "tool_calls",
        ),
        ({"content": "{}"}, "length"),
        ({"refusal": "cannot"}, "stop"),
    ],
)
async def test_bad_output_is_correctable(message, reason):
    async with httpx.AsyncClient(
        base_url="http://model/",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": message, "finish_reason": reason}]}
            )
        ),
    ) as client:
        with pytest.raises(InvalidModelOutput):
            await HttpProvider(client, "fixture-model").next_action(context(), TOOL_SPECS)


async def test_network_timeout_is_runtime_retryable():
    def handle(request):
        raise httpx.ReadTimeout("secret connection details", request=request)

    async with httpx.AsyncClient(
        base_url="http://model/", transport=httpx.MockTransport(handle)
    ) as client:
        provider = HttpProvider(client, "fixture-model")
        with pytest.raises(ProviderError) as error:
            await provider.next_action(context(), TOOL_SPECS)
        assert error.value.retryable and str(error.value) == "MODEL_NETWORK_ERROR"
        assert provider.request_count == 1 and provider.usage_complete is False
