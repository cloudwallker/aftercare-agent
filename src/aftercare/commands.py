"""API 命令事务：幂等记录与业务变更必须同一事务提交。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aftercare.contracts import Actor, canonical_hash
from aftercare.db import Database
from aftercare.models import CommandRequest


class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(code)


def validate_key(key: str | None) -> str:
    if key is None or not 1 <= len(key) <= 128 or any(not 32 <= ord(c) <= 126 for c in key):
        raise DomainError(422, "INVALID_IDEMPOTENCY_KEY", "幂等键须为 1～128 个可打印 ASCII 字符。")
    return key


@dataclass(frozen=True)
class CommandResult:
    status: int
    body: dict[str, Any]
    replayed: bool


async def run_command(
    database: Database,
    actor: Actor,
    route_scope: str,
    key: str,
    payload: dict[str, Any],
    action: Callable[[AsyncSession], Awaitable[tuple[int, dict[str, Any]]]],
) -> CommandResult:
    key = validate_key(key)
    fingerprint = canonical_hash(
        {
            "schema_version": 1,
            "actor": actor.subject,
            "role": actor.role,
            "route_scope": route_scope,
            "payload": payload,
        }
    )
    async with database.sessions.begin() as session:
        inserted = await session.scalar(
            insert(CommandRequest)
            .values(
                id=uuid4(),
                actor_id=actor.subject,
                route_scope=route_scope,
                idempotency_key=key,
                request_hash=fingerprint,
            )
            .on_conflict_do_nothing(constraint="uq_commands_key")
            .returning(CommandRequest.id)
        )
        row = await session.scalar(
            select(CommandRequest).where(
                CommandRequest.actor_id == actor.subject,
                CommandRequest.route_scope == route_scope,
                CommandRequest.idempotency_key == key,
            )
        )
        if inserted is None:
            if row.request_hash != fingerprint:
                raise DomainError(409, "IDEMPOTENCY_CONFLICT", "该幂等键已用于不同请求。")
            if row.response_status is None or row.response_json is None:
                raise RuntimeError("已提交的命令缺少响应")
            return CommandResult(row.response_status, row.response_json, True)
        status, body = await action(session)
        row.response_status, row.response_json = status, body
        return CommandResult(status, body, False)
