"""Chat Completions 工具调用兼容适配器；一次请求，不私自重试。"""

import json

import httpx
from pydantic import ValidationError

from aftercare.agent.provider import InvalidModelOutput, ProviderError, parse_action
from aftercare.contracts import FinalAction
from aftercare.runtime.steps import PayloadTooLarge


def wire_messages(records):
    context = records[0]
    system = (
        context["system_rules"]
        + "\n最终只输出符合以下 JSON Schema 的 JSON 对象："
        + json.dumps(FinalAction.model_json_schema(), ensure_ascii=False)
    )
    result = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(
                {k: v for k, v in context.items() if k != "system_rules"}, ensure_ascii=False
            ),
        },
    ]
    pending = None
    for record in records[1:]:
        output = record["output"]
        if record["kind"] == "model":
            if "error" in output:
                result.append(
                    {
                        "role": "user",
                        "content": "程序校验反馈：" + output["error"] + "。请纠正动作。",
                    }
                )
            elif output.get("kind") == "tool":
                pending = "call_" + record["step_id"].replace("-", "")
                result.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": pending,
                                "type": "function",
                                "function": {
                                    "name": output["name"],
                                    "arguments": json.dumps(output["arguments"]),
                                },
                            }
                        ],
                    }
                )
            else:
                result.append(
                    {"role": "assistant", "content": json.dumps(output, ensure_ascii=False)}
                )
        else:
            if pending is None:
                raise ValueError("缺少持久化模型工具选择")
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": pending,
                    "content": json.dumps(
                        {"step_id": record["step_id"], "result": output}, ensure_ascii=False
                    ),
                }
            )
            pending = None
    if pending is not None:
        raise ValueError("工具结果尚未提交，不能重新请求模型")
    return result


class HttpProvider:
    def __init__(self, client: httpx.AsyncClient, model: str):
        self.client, self.model = client, model
        self.request_count, self.token_count, self.usage_complete = 0, 0, True

    async def next_action(self, messages, tool_specs):
        payload = {
            "model": self.model,
            "messages": wire_messages(messages),
            "tools": [{"type": "function", "function": spec} for spec in tool_specs],
            "parallel_tool_calls": False,
            "stream": False,
        }
        self.request_count += 1
        previous_usage_complete = self.usage_complete
        self.usage_complete = False
        try:
            async with self.client.stream(
                "POST", "chat/completions", json=payload, timeout=30
            ) as response:
                if response.status_code != 200:
                    self.usage_complete = False
                    raise ProviderError(
                        "MODEL_HTTP_ERROR",
                        response.status_code == 429 or response.status_code >= 500,
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 64 * 1024:
                        self.usage_complete = False
                        raise PayloadTooLarge
        except httpx.RequestError:
            self.usage_complete = False
            raise ProviderError("MODEL_NETWORK_ERROR", True) from None
        try:
            document = json.loads(body)
            usage = document.get("usage", {}).get("total_tokens")
            if type(usage) is int and usage >= 0:
                self.token_count += usage
                self.usage_complete = previous_usage_complete
            else:
                self.usage_complete = False
            choices = document["choices"]
            if len(choices) != 1:
                raise ValueError
            choice = choices[0]
            message = choice["message"]
            if choice["finish_reason"] not in {"stop", "tool_calls"} or message.get("refusal"):
                raise ValueError
            calls = message.get("tool_calls") or []
            if calls:
                if len(calls) != 1 or calls[0]["type"] != "function":
                    raise ValueError
                function = calls[0]["function"]
                action = {
                    "kind": "tool",
                    "name": function["name"],
                    "arguments": json.loads(function["arguments"]),
                }
            else:
                action = json.loads(message["content"])
                if not isinstance(action, dict) or action.get("kind") != "final":
                    raise ValueError
            return parse_action(action)
        except (ValueError, KeyError, TypeError, AttributeError, ValidationError):
            raise InvalidModelOutput("INVALID_MODEL_OUTPUT") from None
