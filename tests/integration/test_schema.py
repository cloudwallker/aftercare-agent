"""验证真实 PostgreSQL 迁移、角色边界和订单级防重。"""

from uuid import uuid4

import pytest
from sqlalchemy import CHAR, Text, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = pytest.mark.asyncio


async def test_migration_head_and_eight_tables(db, merchant_db):
    expected = {
        "app": {"tasks", "steps", "approvals", "refund_operations", "events", "command_requests"},
        "merchant": {"orders", "refund_requests"},
    }
    for schema, session in (("app", db), ("merchant", merchant_db)):
        assert (
            await session.scalar(text("SELECT version_num FROM public.alembic_version"))
            == "0001_initial"
        )
        tables = await session.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname=:schema"), {"schema": schema}
        )
        assert set(tables) == expected[schema]


async def test_app_role_cannot_read_merchant(db):
    with pytest.raises(DBAPIError) as error:
        await db.execute(text("SELECT id FROM merchant.orders LIMIT 1"))
    assert error.value.orig.sqlstate == "42501"


async def test_merchant_role_cannot_read_app(merchant_db):
    with pytest.raises(DBAPIError) as error:
        await merchant_db.execute(text("SELECT id FROM app.tasks LIMIT 1"))
    assert error.value.orig.sqlstate == "42501"


async def test_roles_cannot_write_migration_metadata(db, merchant_db):
    for session in (db, merchant_db):
        with pytest.raises(DBAPIError) as error:
            await session.execute(text("UPDATE public.alembic_version SET version_num=version_num"))
        assert error.value.orig.sqlstate == "42501"


async def test_business_connections_are_isolated_utc_and_unprivileged(db, merchant_db):
    for session, role in ((db, "aftercare_app"), (merchant_db, "aftercare_merchant")):
        assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
        assert await session.scalar(text("SHOW timezone")) == "UTC"
        actual = (
            await session.execute(
                text("SELECT rolname, rolsuper FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
        assert actual.rolname == role
        assert actual.rolsuper is False


async def insert_operation(db, order_id, status="prepared"):
    task, approval, operation = uuid4(), uuid4(), uuid4()
    await db.execute(
        text(
            "INSERT INTO app.tasks (id, customer_id, order_id, message) VALUES"
            " (:id,'customer',:order,'申请退款')"
        ),
        {"id": task, "order": order_id},
    )
    await db.execute(
        text(
            "INSERT INTO app.approvals "
            "(id,task_id,generation,status,payload,payload_hash,summary,expires_at)"
            " VALUES "
            "(:id,:task,1,'approved','{}',:hash,'审批',clock_timestamp()+interval"
            " '1 day')"
        ),
        {"id": approval, "task": task, "hash": "a" * 64},
    )
    await db.execute(
        text(
            "INSERT INTO app.refund_operations "
            "(id,task_id,approval_id,order_id,generation,operation_key,request,request_hash,status,reconcile_deadline)"
            " VALUES "
            "(:id,:task,:approval,:order,1,:key,'{}',:hash,:status,clock_timestamp()+interval"
            " '1 hour')"
        ),
        {
            "id": operation,
            "task": task,
            "approval": approval,
            "order": order_id,
            "key": f"refund:{task}:1",
            "hash": "b" * 64,
            "status": status,
        },
    )
    return operation, approval


@pytest.mark.parametrize("status", ["prepared", "unknown", "confirmed"])
async def test_local_active_operation_reserves_order(db, status):
    order_id = str(uuid4())
    await insert_operation(db, order_id, status)
    with pytest.raises(IntegrityError) as error:
        async with db.begin_nested():
            await insert_operation(db, order_id)
    assert error.value.orig.sqlstate == "23505"
    assert error.value.orig.diag.constraint_name == "uq_operations_reserved_order"


async def test_declined_operation_releases_order(db):
    order_id = str(uuid4())
    await insert_operation(db, order_id, "declined")
    await insert_operation(db, order_id)


async def insert_refund(db, order_id, status):
    await db.execute(
        text(
            "INSERT INTO merchant.refund_requests "
            "(operation_key,request_hash,request,order_id,status,refund_id) "
            "VALUES (:key,:hash,'{}',:order,:status,:refund)"
        ),
        {
            "key": str(uuid4()),
            "hash": "c" * 64,
            "order": order_id,
            "status": status,
            "refund": uuid4() if status == "confirmed" else None,
        },
    )


async def test_merchant_confirmed_order_is_unique(merchant_db):
    order_id = str(uuid4())
    await insert_refund(merchant_db, order_id, "confirmed")
    with pytest.raises(IntegrityError) as error:
        async with merchant_db.begin_nested():
            await insert_refund(merchant_db, order_id, "confirmed")
    assert error.value.orig.sqlstate == "23505"
    assert error.value.orig.diag.constraint_name == "uq_refund_requests_confirmed_order"


async def test_merchant_declined_unknown_order_has_no_foreign_key(merchant_db):
    order_id = str(uuid4())
    await insert_refund(merchant_db, order_id, "declined")
    await insert_refund(merchant_db, order_id, "declined")


@pytest.mark.parametrize(
    "column,value",
    [
        ("operation_key", "changed"),
        ("request", {"changed": True}),
        ("request_hash", "d" * 64),
    ],
)
async def test_frozen_operation_is_immutable(db, column, value):
    operation, _ = await insert_operation(db, str(uuid4()))
    column_types = {"operation_key": Text(), "request": JSONB(), "request_hash": CHAR(64)}
    assert column in column_types
    statement = text(f"UPDATE app.refund_operations SET {column}=:value WHERE id=:id").bindparams(
        bindparam("value", type_=column_types[column])
    )
    with pytest.raises(DBAPIError) as error:
        await db.execute(
            statement,
            {"id": operation, "value": value},
        )
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.message_primary == "refund operation request is immutable"


async def test_approval_payload_is_immutable(db):
    _, approval = await insert_operation(db, str(uuid4()))
    with pytest.raises(DBAPIError) as error:
        await db.execute(
            text("UPDATE app.approvals SET summary='改写方案' WHERE id=:id"), {"id": approval}
        )
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.message_primary == "approval payload is immutable"


async def test_running_task_requires_lease(db):
    with pytest.raises(IntegrityError) as error:
        await db.execute(
            text(
                "INSERT INTO app.tasks (id,customer_id,order_id,message,status) "
                "VALUES (:id,'c','o','m','running')"
            ),
            {"id": uuid4()},
        )
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.constraint_name == "ck_tasks_lease"
