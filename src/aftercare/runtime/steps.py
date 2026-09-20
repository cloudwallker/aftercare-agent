"""步骤调用前预扣预算；调用完成后原子保存结果、checkpoint 与事件。"""

import copy
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from aftercare.contracts import Lease
from aftercare.db import Database
from aftercare.events import append_event
from aftercare.models import Step
from aftercare.runtime.leases import locked_task

TOOLS = frozenset({"get_order", "get_shipment", "get_refund_policy"})


class BudgetExceeded(Exception):
    def __init__(self, code="INVESTIGATION_BUDGET_EXCEEDED"):
        self.code = code
        super().__init__(code)


class StepConflict(Exception):
    """不能替换已持久化选择，也不能提交较旧的调用结果。"""


class PayloadTooLarge(Exception):
    code = "PAYLOAD_TOO_LARGE"


def bounded_json(value: dict, limit: int) -> dict:
    if not isinstance(value, dict):
        raise ValueError("步骤数据必须为 JSON 对象")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > limit:
        raise PayloadTooLarge
    return json.loads(encoded)


@dataclass(frozen=True)
class StepAttempt:
    id: UUID
    generation: int
    step_no: int
    kind: str
    name: str
    attempt_count: int
    timeout_seconds: float
    reused: bool = False
    output: dict[str, Any] | None = None


def remaining_seconds(task, now) -> float:
    if task.generation_started_at is None:
        raise StepConflict("本代尚未领取")
    remaining = 180 - (now - task.generation_started_at).total_seconds()
    if remaining <= 0:
        raise BudgetExceeded("INVESTIGATION_TIMEOUT")
    return remaining


async def reserve_step(
    database: Database,
    lease: Lease,
    *,
    kind: str,
    name: str,
    input: dict,
    step_no: int | None = None,
) -> StepAttempt:
    if not (kind == "model" and name == "next_action" or kind == "tool" and name in TOOLS):
        raise StepConflict("只允许模型动作与白名单只读工具")
    data = bounded_json(input, 128 * 1024 if kind == "model" else 16 * 1024)
    async with database.sessions.begin() as session:
        task, now = await locked_task(session, lease)
        if task.phase != "investigate":
            raise StepConflict("退款阶段不能使用通用步骤")
        checkpoint = task.checkpoint
        number = checkpoint["next_step_no"] if step_no is None else step_no
        if type(number) is not int or number < 1:
            raise StepConflict("步骤序号无效")
        step = await session.scalar(
            select(Step)
            .where(
                Step.task_id == task.id, Step.generation == task.generation, Step.step_no == number
            )
            .with_for_update()
        )
        if step is not None:
            if (step.kind, step.name, step.input) != (kind, name, data):
                raise StepConflict("不能替换已持久化步骤")
            if step.status == "succeeded":
                return StepAttempt(
                    step.id,
                    step.generation,
                    number,
                    kind,
                    name,
                    step.attempt_count,
                    0,
                    True,
                    copy.deepcopy(step.output),
                )
            if step.status != "pending":
                raise StepConflict("步骤已经失败")
        if number != checkpoint["next_step_no"]:
            raise StepConflict("只能预扣当前待完成步骤")
        remaining = remaining_seconds(task, now)
        if checkpoint["invalid_output_count"] >= 2:
            raise BudgetExceeded("INVALID_MODEL_OUTPUT")
        if kind == "model" and task.model_call_count >= 8:
            raise BudgetExceeded("MODEL_BUDGET_EXCEEDED")
        if step is None:
            if kind == "tool":
                if task.tool_call_count >= 6:
                    raise BudgetExceeded("TOOL_BUDGET_EXCEEDED")
                task.tool_call_count += 1
            step = Step(
                id=uuid4(),
                task_id=task.id,
                generation=task.generation,
                step_no=number,
                kind=kind,
                name=name,
                input=data,
                attempt_count=0,
                status="pending",
            )
            session.add(step)
        remote = name != "get_refund_policy"
        if remote and step.attempt_count >= 3:
            raise BudgetExceeded("STEP_ATTEMPTS_EXHAUSTED")
        if remote:
            step.attempt_count += 1
        if kind == "model":
            task.model_call_count += 1
        task.updated_at = now
        await append_event(
            session,
            task,
            "step.started",
            {
                "step_id": str(step.id),
                "kind": kind,
                "name": name,
                "attempt_count": step.attempt_count,
            },
        )
        return StepAttempt(
            step.id,
            task.generation,
            number,
            kind,
            name,
            step.attempt_count,
            min(30 if kind == "model" else 5, remaining),
        )


async def complete_step(
    database: Database, lease: Lease, attempt: StepAttempt, output: dict, *, invalid_output=False
) -> None:
    data = bounded_json(output, 8 * 1024 if attempt.kind == "model" else 16 * 1024)
    async with database.sessions.begin() as session:
        task, now = await locked_task(session, lease)
        step = await session.scalar(select(Step).where(Step.id == attempt.id).with_for_update())
        if (
            step is None
            or step.task_id != task.id
            or step.generation != task.generation
            or step.generation != attempt.generation
            or step.kind != attempt.kind
            or step.name != attempt.name
            or step.step_no != attempt.step_no
            or step.attempt_count != attempt.attempt_count
        ):
            raise StepConflict("结果不属于当前步骤尝试")
        if step.status == "succeeded":
            if step.output != data:
                raise StepConflict("不能改写已提交结果")
            return
        if (
            step.status != "pending"
            or task.phase != "investigate"
            or step.step_no != task.checkpoint["next_step_no"]
        ):
            raise StepConflict("步骤不是当前待完成步骤")
        remaining_seconds(task, now)
        checkpoint = copy.deepcopy(task.checkpoint)
        if invalid_output:
            if step.kind != "model":
                raise StepConflict("只有模型步骤能记录格式纠正")
            checkpoint["invalid_output_count"] += 1
        checkpoint["messages"].append(
            {"step_id": str(step.id), "kind": step.kind, "name": step.name, "output": data}
        )
        if step.kind == "tool":
            checkpoint["evidence_by_tool"][step.name] = str(step.id)
        checkpoint["next_step_no"] += 1
        task.checkpoint = bounded_json(checkpoint, 128 * 1024)
        task.updated_at = now
        step.output, step.status, step.completed_at = data, "succeeded", now
        await append_event(session, task, "step.completed", {"step_id": str(step.id)})


async def record_invalid_output(database: Database, lease: Lease) -> bool:
    """返回是否还可纠正一次；计数跨进程恢复保留。"""
    async with database.sessions.begin() as session:
        task, now = await locked_task(session, lease)
        if task.phase != "investigate":
            raise StepConflict("不是调查阶段")
        checkpoint = copy.deepcopy(task.checkpoint)
        checkpoint["invalid_output_count"] += 1
        task.checkpoint = checkpoint
        task.updated_at = now
        await append_event(session, task, "step.failed", {"error_code": "INVALID_MODEL_OUTPUT"})
        return checkpoint["invalid_output_count"] <= 1
