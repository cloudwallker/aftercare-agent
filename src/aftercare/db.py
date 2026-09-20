"""业务连接固定 READ COMMITTED 与 UTC；调用者控制短事务边界。"""

from dataclasses import dataclass

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@dataclass(frozen=True)
class Database:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


def create_database(url: str) -> Database:
    parsed = make_url(url)
    if parsed.drivername != "postgresql+psycopg":
        raise ValueError("仅支持 PostgreSQL psycopg 连接")
    engine = create_async_engine(
        parsed,
        isolation_level="READ COMMITTED",
        pool_pre_ping=True,
        connect_args={"options": "-c timezone=UTC", "connect_timeout": 5},
    )
    return Database(engine, async_sessionmaker(engine, expire_on_commit=False))


def validate_test_database_url(value: str) -> URL:
    url = make_url(value)
    if url.drivername != "postgresql+psycopg" or not (url.database or "").endswith("_test"):
        raise ValueError("测试必须显式连接名称以 _test 结尾的 PostgreSQL 数据库")
    return url
