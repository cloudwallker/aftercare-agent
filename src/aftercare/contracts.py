"""纯数据契约；不依赖 FastAPI、ORM 或进程配置。"""

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

Generation = Annotated[StrictInt, Field(ge=1, le=3)]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class Actor:
    subject: str
    role: Literal["client", "reviewer"]


@dataclass(frozen=True)
class Lease:
    task_id: UUID
    worker_id: str
    epoch: int


class CreateTask(Contract):
    order_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("order_id", "message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value


class ApprovalDecision(Contract):
    generation: Generation
    payload_hash: Hash
    decision: Literal["approve", "reject"]


class ReassessRequest(Contract):
    expected_generation: Generation


class EmptyArguments(Contract):
    pass


class ToolAction(Contract):
    kind: Literal["tool"] = "tool"
    name: Literal["get_order", "get_shipment", "get_refund_policy"]
    arguments: EmptyArguments


class FinalAction(Contract):
    kind: Literal["final"] = "final"
    recommendation: Literal["recommend_refund", "no_action"]
    summary: str = Field(min_length=1, max_length=500)
    evidence_step_ids: list[UUID] = Field(max_length=6)

    @field_validator("evidence_step_ids")
    @classmethod
    def unique_evidence(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("证据 ID 不得重复")
        return value


class RefundRequest(Contract):
    order_id: str = Field(min_length=1, max_length=128)
    customer_id: str = Field(min_length=1, max_length=128)
    amount_cents: PositiveInt
    currency: str = Field(min_length=1, max_length=8)
    expected_order_version: PositiveInt
    policy_version: str = Field(min_length=1, max_length=128)
    authorization_id: UUID


class ConfirmedRefund(Contract):
    status: Literal["confirmed"]
    operation_key: str
    request_hash: Hash
    order_id: str
    amount_cents: PositiveInt
    currency: Literal["CNY"]
    refund_id: UUID


class DeclinedRefund(Contract):
    status: Literal["declined"]
    operation_key: str
    request_hash: Hash
    error_code: Literal["ORDER_CHANGED", "POLICY_DENIED", "ALREADY_REFUNDED", "ORDER_NOT_FOUND"]


class CreateResult(Contract):
    task_id: UUID
    status: Literal["queued"] = "queued"
    replayed: bool = Field(default=False, exclude=True)


class DecisionResult(Contract):
    approval_id: UUID
    status: Literal["approved", "rejected"]
    generation: Generation
    task_id: UUID
    replayed: bool = Field(default=False, exclude=True)


class ReassessResult(Contract):
    task_id: UUID
    accepted_generation: Generation
    replayed: bool = Field(default=False, exclude=True)


def canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
