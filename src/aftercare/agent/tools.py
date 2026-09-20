"""模型只能传空参数；订单和客户来自任务上下文。"""

from aftercare.contracts import ToolAction
from aftercare.refunds.policy import POLICY

TOOL_SPECS = [
    {
        "name": name,
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    for name in ("get_order", "get_shipment", "get_refund_policy")
]


async def invoke_tool(
    action: ToolAction, merchant, order_id: str, customer_id: str, timeout: float
):
    if action.name == "get_refund_policy":
        return dict(POLICY)
    return await merchant.read(action.name, order_id, customer_id, timeout)
