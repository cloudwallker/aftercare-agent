"""离线确定性演示，使用与真实 Provider 相同的动作接口。"""

from aftercare.agent.tools import TOOL_SPECS
from aftercare.contracts import FinalAction, ToolAction


class MockProvider:
    def __init__(self, recommendation=None):
        self.recommendation = recommendation

    async def next_action(self, messages, tool_specs):
        results = {m["name"]: m for m in messages if m.get("kind") == "tool"}
        for spec in TOOL_SPECS:
            if spec["name"] not in results:
                return ToolAction(name=spec["name"], arguments={})
        # 版本不一致反馈后重新获取两份快照；仍然受六次工具预算限制。
        feedback = next((m for m in reversed(messages) if m.get("kind") == "model"), {})
        if feedback.get("output", {}).get("error") == "INCONSISTENT_SNAPSHOT":
            return ToolAction(name="get_order", arguments={})
        if messages[-1].get("name") == "get_order":
            return ToolAction(name="get_shipment", arguments={})
        message = messages[0]["message"]
        recommendation = self.recommendation or (
            "recommend_refund" if "退款" in message else "no_action"
        )
        return FinalAction(
            recommendation=recommendation,
            summary="已完成订单、物流及退款规则查询。",
            evidence_step_ids=[results[s["name"]]["step_id"] for s in TOOL_SPECS],
        )
