"""按进程角色读取环境；不在导入时读取密钥或建立连接。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url


@dataclass(frozen=True)
class DatabaseSettings:
    role: str
    database_url: str = field(repr=False)


@dataclass(frozen=True)
class ApiSettings(DatabaseSettings):
    client_token: str = field(repr=False)
    reviewer_token: str = field(repr=False)


@dataclass(frozen=True)
class MerchantSettings(DatabaseSettings):
    merchant_token: str = field(repr=False)


@dataclass(frozen=True)
class WorkerSettings(MerchantSettings):
    merchant_base_url: str
    model_mode: Literal["mock", "http"] = "mock"
    model_base_url: str | None = None
    model_api_key: str | None = field(default=None, repr=False)
    model_name: str | None = None
    worker_concurrency: int = 4
    lease_seconds: int = 20
    heartbeat_seconds: int = 5


Settings = ApiSettings | WorkerSettings | MerchantSettings | DatabaseSettings


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ

    def required(name: str) -> str:
        value = env.get(name, "").strip()
        if not value:
            raise ValueError(f"缺少配置字段 {name}")
        return value

    def database(name: str, username: str | None) -> str:
        value = required(name)
        try:
            url = make_url(value)
        except Exception:
            raise ValueError(f"{name} 不是有效数据库连接") from None
        if (
            url.drivername != "postgresql+psycopg"
            or not url.database
            or not url.host
            or not url.username
        ):
            raise ValueError(f"{name} 必须为 PostgreSQL psycopg 连接")
        if username is not None and url.username != username:
            raise ValueError(f"{name} 必须使用业务角色 {username}")
        return value

    def http_url(name: str) -> str:
        value = required(name)
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"{name} 必须为不含凭据的 HTTP(S) 服务地址")
        return value.rstrip("/")

    def positive_int(name: str, default: int) -> int:
        try:
            value = int(env.get(name, str(default)))
        except ValueError:
            raise ValueError(f"{name} 必须为正整数") from None
        if value <= 0:
            raise ValueError(f"{name} 必须为正整数")
        return value

    role = required("APP_ROLE")
    # 在拥有多个 Token 的配置入口验证互异，返回对象仅保留本角色需要的子集。
    tokens = [
        env[k].strip() for k in ("CLIENT_TOKEN", "REVIEWER_TOKEN", "MERCHANT_TOKEN") if env.get(k)
    ]
    if len(set(tokens)) != len(tokens):
        raise ValueError("CLIENT_TOKEN、REVIEWER_TOKEN、MERCHANT_TOKEN 必须互不相同")
    if role == "api":
        return ApiSettings(
            role,
            database("APP_DATABASE_URL", "aftercare_app"),
            required("CLIENT_TOKEN"),
            required("REVIEWER_TOKEN"),
        )
    if role in {"merchant", "seed"}:
        url = database("MERCHANT_DATABASE_URL", "aftercare_merchant")
        if role == "seed":
            return DatabaseSettings(role, url)
        return MerchantSettings(role, url, required("MERCHANT_TOKEN"))
    if role == "migrate":
        return DatabaseSettings(role, database("MIGRATION_DATABASE_URL", None))
    if role != "worker":
        raise ValueError("APP_ROLE 必须为 api/worker/merchant/migrate/seed")
    url = database("APP_DATABASE_URL", "aftercare_app")
    token = required("MERCHANT_TOKEN")
    merchant_url = http_url("MERCHANT_BASE_URL")
    mode = env.get("MODEL_MODE", "mock")
    if mode not in {"mock", "http"}:
        raise ValueError("MODEL_MODE 必须为 mock/http")
    model_url = http_url("MODEL_BASE_URL") if mode == "http" else None
    model_key = required("MODEL_API_KEY") if mode == "http" else None
    model_name = required("MODEL_NAME") if mode == "http" else None
    lease = positive_int("LEASE_SECONDS", 20)
    heartbeat = positive_int("HEARTBEAT_SECONDS", 5)
    if heartbeat >= lease:
        raise ValueError("HEARTBEAT_SECONDS 必须小于 LEASE_SECONDS")
    return WorkerSettings(
        role,
        url,
        token,
        merchant_url,
        mode,
        model_url,
        model_key,
        model_name,
        positive_int("WORKER_CONCURRENCY", 4),
        lease,
        heartbeat,
    )
