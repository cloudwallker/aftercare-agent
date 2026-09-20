"""商家独立映射；不导入应用 ORM 或审批状态。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CHAR, BigInteger, CheckConstraint, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, datetime: DateTime(timezone=True)}


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "paid_amount_cents > 0 AND delay_days >= 0 AND order_version > 0",
            name="ck_orders_values",
        ),
        {"schema": "merchant"},
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text)
    paid_amount_cents: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(Text)
    payment_status: Mapped[str] = mapped_column(Text)
    shipment_status: Mapped[str] = mapped_column(Text)
    delay_days: Mapped[int] = mapped_column(server_default="0")
    refunded: Mapped[bool] = mapped_column(server_default=text("false"))
    order_version: Mapped[int] = mapped_column(BigInteger, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(server_default=text("clock_timestamp()"))


class RefundRequest(Base):
    __tablename__ = "refund_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('processing','confirmed','declined')", name="ck_refund_requests_status"
        ),
        Index(
            "uq_refund_requests_confirmed_order",
            "order_id",
            unique=True,
            postgresql_where=text("status = 'confirmed'"),
        ),
        {"schema": "merchant"},
    )
    operation_key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(CHAR(64))
    request: Mapped[dict[str, Any]]
    order_id: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    refund_id: Mapped[UUID | None] = mapped_column(unique=True)
    response: Mapped[dict[str, Any] | None]
    response_http_status: Mapped[int | None]
    fault_consumed: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("clock_timestamp()"))
    completed_at: Mapped[datetime | None]
