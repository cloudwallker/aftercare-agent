"""Provider 返回单一动作；非法响应不会直接进入业务状态机。"""

from typing import Annotated, Protocol

from pydantic import Field, TypeAdapter

from aftercare.contracts import FinalAction, ToolAction

ACTION = TypeAdapter(Annotated[ToolAction | FinalAction, Field(discriminator="kind")])


class ProviderError(Exception):
    def __init__(self, code="MODEL_UNAVAILABLE", retryable=True):
        self.code, self.retryable = code, retryable
        super().__init__(code)


class InvalidModelOutput(Exception):
    """协议或格式错误消耗持久化纠正机会，不作为网络错误重试。"""


class ModelProvider(Protocol):
    async def next_action(
        self, messages: list[dict], tool_specs: list[dict]
    ) -> ToolAction | FinalAction: ...


def parse_action(value):
    if isinstance(value, (ToolAction, FinalAction)):
        value = value.model_dump(mode="json")
    return (
        ACTION.validate_json(value)
        if isinstance(value, (str, bytes))
        else ACTION.validate_python(value)
    )
