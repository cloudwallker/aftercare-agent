"""迁移只接受显式管理员连接，业务进程不需要管理员凭据。"""

import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from aftercare.models import Base as AppBase
from mock_merchant.models import Base as MerchantBase

config = context.config
target_metadata = [AppBase.metadata, MerchantBase.metadata]
url = os.environ.get("MIGRATION_DATABASE_URL")
if not url:
    raise RuntimeError("必须配置 MIGRATION_DATABASE_URL")
parsed = make_url(url)
if parsed.username != "postgres" or parsed.drivername != "postgresql+psycopg":
    raise RuntimeError("迁移必须使用 postgresql+psycopg 管理员 postgres 连接")

if context.is_offline_mode():
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        version_table_schema="public",
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema="public",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
