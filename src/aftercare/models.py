"""应用 schema 映射；业务函数自行控制事务。"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, datetime: DateTime(timezone=True)}


def now_column():
    return mapped_column(DateTime(timezone=True), server_default=text("clock_timestamp()"))


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN "
            "('queued','running','waiting_approval','succeeded','rejected','failed','manual_review')",
            name="ck_tasks_status",
        ),
        CheckConstraint("phase IN ('investigate','execute_refund')", name="ck_tasks_phase"),
        CheckConstraint("generation BETWEEN 1 AND 3", name="ck_tasks_generation"),
        CheckConstraint(
            "model_call_count >= 0 AND tool_call_count >= 0 AND lease_epoch >="
            " 0 AND last_event_seq >= 0",
            name="ck_tasks_counters",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL AND "
            "lease_expires_at IS NOT NULL) OR (status <> 'running' AND "
            "lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_tasks_lease",
        ),
        Index("ix_tasks_queue", "status", "next_run_at", "created_at"),
        Index("ix_tasks_lease", "status", "lease_expires_at"),
        Index("ix_tasks_customer", "customer_id", "created_at", "id"),
        {"schema": "app"},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    customer_id: Mapped[str] = mapped_column(Text)
    order_id: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    phase: Mapped[str] = mapped_column(Text, server_default="investigate")
    generation: Mapped[int] = mapped_column(server_default="1")
    checkpoint: Mapped[dict[str, Any]] = mapped_column(
        server_default=text(
            '\'{"schema_version": 1,"generation": '
            '1,"messages":[],"next_step_no": '
            '1,"evidence_by_tool":{},"invalid_output_count": 0}\'::jsonb'
        )
    )
    model_call_count: Mapped[int] = mapped_column(server_default="0")
    tool_call_count: Mapped[int] = mapped_column(server_default="0")
    generation_started_at: Mapped[datetime | None]
    next_run_at: Mapped[datetime] = now_column()
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, server_default="0")
    lease_expires_at: Mapped[datetime | None]
    last_event_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")
    result: Mapped[dict[str, Any] | None]
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = now_column()
    updated_at: Mapped[datetime] = now_column()


class Step(Base):
    __tablename__ = "steps"
    __table_args__ = (
        UniqueConstraint("task_id", "generation", "step_no", name="uq_steps_position"),
        CheckConstraint(
            "generation BETWEEN 1 AND 3 AND step_no > 0 AND attempt_count >= 0",
            name="ck_steps_counters",
        ),
        CheckConstraint("kind IN ('model','tool')", name="ck_steps_kind"),
        CheckConstraint("status IN ('pending','succeeded','failed')", name="ck_steps_status"),
        {"schema": "app"},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("app.tasks.id"))
    generation: Mapped[int]
    step_no: Mapped[int]
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    attempt_count: Mapped[int] = mapped_column(server_default="0")
    input: Mapped[dict[str, Any]]
    output: Mapped[dict[str, Any] | None]
    error_code: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = now_column()
    completed_at: Mapped[datetime | None]


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("task_id", "generation", name="uq_approvals_generation"),
        CheckConstraint("generation BETWEEN 1 AND 3", name="ck_approvals_generation"),
        CheckConstraint(
            "status IN ('pending','approved','rejected','expired','invalidated')",
            name="ck_approvals_status",
        ),
        {"schema": "app"},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("app.tasks.id"))
    generation: Mapped[int]
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    payload: Mapped[dict[str, Any]]
    payload_hash: Mapped[str] = mapped_column(CHAR(64))
    summary: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime]
    decision_key: Mapped[str | None] = mapped_column(Text)
    decision_hash: Mapped[str | None] = mapped_column(CHAR(64))
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = now_column()


class RefundOperation(Base):
    __tablename__ = "refund_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('prepared','unknown','confirmed','declined')", name="ck_operations_status"
        ),
        CheckConstraint(
            "generation BETWEEN 1 AND 3 AND dispatch_count >= 0 AND reconcile_count >= 0",
            name="ck_operations_counters",
        ),
        Index(
            "uq_operations_reserved_order",
            "order_id",
            unique=True,
            postgresql_where=text("status IN ('prepared','unknown','confirmed')"),
        ),
        {"schema": "app"},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("app.tasks.id"), unique=True)
    approval_id: Mapped[UUID] = mapped_column(ForeignKey("app.approvals.id"), unique=True)
    order_id: Mapped[str] = mapped_column(Text)
    generation: Mapped[int]
    operation_key: Mapped[str] = mapped_column(Text, unique=True)
    request: Mapped[dict[str, Any]]
    request_hash: Mapped[str] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(Text, server_default="prepared")
    dispatch_count: Mapped[int] = mapped_column(server_default="0")
    reconcile_count: Mapped[int] = mapped_column(server_default="0")
    reconcile_deadline: Mapped[datetime]
    merchant_refund_id: Mapped[UUID | None]
    merchant_result: Mapped[dict[str, Any] | None]
    last_error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = now_column()
    updated_at: Mapped[datetime] = now_column()


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (CheckConstraint("seq > 0", name="ck_events_seq"), {"schema": "app"})
    task_id: Mapped[UUID] = mapped_column(ForeignKey("app.tasks.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = now_column()


class CommandRequest(Base):
    __tablename__ = "command_requests"
    __table_args__ = (
        UniqueConstraint("actor_id", "route_scope", "idempotency_key", name="uq_commands_key"),
        {"schema": "app"},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_id: Mapped[str] = mapped_column(Text)
    route_scope: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(CHAR(64))
    response_status: Mapped[int | None]
    response_json: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = now_column()
