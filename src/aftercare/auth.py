"""本地演示 Token 鉴权；身份从服务端配置派生。"""

import secrets

from aftercare.commands import DomainError
from aftercare.config import ApiSettings
from aftercare.contracts import Actor


def authenticate(
    settings: ApiSettings, authorization: str | None, actors: dict[str, Actor] | None = None
) -> Actor:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer":
        raise DomainError(401, "UNAUTHORIZED", "需要有效身份凭据。")
    roles = actors or {
        "client": Actor("customer_demo", "client"),
        "reviewer": Actor("reviewer_demo", "reviewer"),
    }
    for role, expected in (
        ("client", settings.client_token),
        ("reviewer", settings.reviewer_token),
    ):
        if secrets.compare_digest(token.encode(), expected.encode()):
            return roles[role]
    raise DomainError(401, "UNAUTHORIZED", "需要有效身份凭据。")


def require_client(actor: Actor) -> None:
    if actor.role != "client":
        raise DomainError(403, "FORBIDDEN", "此操作仅允许工单提交者使用。")
