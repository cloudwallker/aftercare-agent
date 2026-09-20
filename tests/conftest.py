"""集成测试只连接显式指定的独立测试库。"""

import asyncio
import os
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

from aftercare.db import create_database

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def database_url(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"必须显式配置 {name}，且数据库名以 _test 结尾；不能使用开发库")
    url = make_url(value)
    if not url.database or not url.database.endswith("_test"):
        pytest.fail(f"{name} 必须指向以 _test 结尾的独立测试库")
    expected = "aftercare_app" if name == "TEST_APP_DATABASE_URL" else "aftercare_merchant"
    if url.username != expected or url.drivername != "postgresql+psycopg":
        pytest.fail(f"{name} 必须使用 postgresql+psycopg 和 {expected} 角色")
    return value


@pytest_asyncio.fixture
async def db():
    database = create_database(database_url("TEST_APP_DATABASE_URL"))
    try:
        async with database.sessions() as session:
            yield session
            await session.rollback()
    finally:
        await database.engine.dispose()


@pytest_asyncio.fixture
async def merchant_db():
    database = create_database(database_url("TEST_MERCHANT_DATABASE_URL"))
    try:
        async with database.sessions() as session:
            yield session
            await session.rollback()
    finally:
        await database.engine.dispose()


@pytest.fixture
def merchant_test_ids():
    """只登记本次测试创建的对象，管理员清理不得扩大到整个 schema。"""
    return []


@pytest.fixture
def new_order_id(merchant_test_ids):
    def allocate(value=None):
        order_id = value or f"TEST-{uuid4()}"
        merchant_test_ids.append(order_id)
        return order_id

    return allocate


@pytest_asyncio.fixture
async def merchant_database(merchant_test_ids):
    from sqlalchemy import delete

    from aftercare.db import validate_test_database_url
    from mock_merchant.models import Order, RefundRequest

    url = database_url("TEST_MERCHANT_DATABASE_URL")
    admin_value = os.environ.get("TEST_MIGRATION_DATABASE_URL")
    if not admin_value:
        pytest.fail("提交事务的测试必须显式配置 TEST_MIGRATION_DATABASE_URL 以清理本测试对象")
    admin_url = validate_test_database_url(admin_value)
    business_url = make_url(url)
    if (admin_url.host, admin_url.port, admin_url.database) != (
        business_url.host,
        business_url.port,
        business_url.database,
    ) or admin_url.username != "postgres":
        pytest.fail("测试管理员必须连接同一显式 _test 数据库")
    database = create_database(url)
    admin = create_database(admin_value)
    try:
        yield database
    finally:
        try:
            if merchant_test_ids and os.environ.get("AFTERCARE_KEEP_DEMO") != "1":
                async with admin.sessions.begin() as session:
                    await session.execute(
                        delete(RefundRequest).where(RefundRequest.order_id.in_(merchant_test_ids))
                    )
                    await session.execute(delete(Order).where(Order.id.in_(merchant_test_ids)))
        finally:
            await database.engine.dispose()
            await admin.engine.dispose()


@pytest.fixture
def seed_order(merchant_database, new_order_id):
    async def create(**overrides):
        from mock_merchant.models import Order

        data = {
            "id": new_order_id(),
            "customer_id": "customer_demo",
            "paid_amount_cents": 19900,
            "currency": "CNY",
            "payment_status": "paid",
            "shipment_status": "delayed",
            "delay_days": 8,
            "refunded": False,
            "order_version": 1,
        }
        if "id" in overrides:
            new_order_id(overrides["id"])
        data.update(overrides)
        async with merchant_database.sessions.begin() as session:
            session.add(Order(**data))
        return SimpleNamespace(**data)

    return create


@pytest.fixture
def pending_approval(app_database, app_actor, create_task):
    """直接建立不可变审批快照供并发/期限测试；真实调查链路另行验证。"""

    async def create(order_id=None, *, seconds=86400, generation=1, amount=19900):
        from datetime import timedelta

        from sqlalchemy import func, select

        from aftercare.contracts import canonical_hash
        from aftercare.models import Approval, Task

        created = await create_task(**({"order_id": order_id} if order_id else {}))
        async with app_database.sessions.begin() as session:
            task = await session.get(Task, created.task_id)
            now = await session.scalar(select(func.clock_timestamp()))
            task.status, task.generation = "waiting_approval", generation
            payload = {
                "task_id": str(task.id),
                "generation": generation,
                "customer_id": app_actor.subject,
                "order_id": task.order_id,
                "amount_cents": amount,
                "currency": "CNY",
                "order_version": 1,
                "policy_version": "refund_v1",
                "evidence_step_ids": sorted(str(uuid4()) for _ in range(3)),
            }
            approval = Approval(
                id=uuid4(),
                task_id=task.id,
                generation=generation,
                payload=payload,
                payload_hash=canonical_hash(payload),
                summary="测试退款方案",
                expires_at=now + timedelta(seconds=seconds),
            )
            session.add(approval)
        return approval

    return create


@pytest.fixture
def approved_refund(pending_approval, app_database, app_actor, seed_order):
    async def create(**kwargs):
        from aftercare.approvals import decide_approval
        from aftercare.contracts import Actor, ApprovalDecision

        order = await seed_order(customer_id=app_actor.subject)
        approval = await pending_approval(order.id, **kwargs)
        await decide_approval(
            app_database,
            Actor(f"reviewer-{app_actor.subject}", "reviewer"),
            approval.id,
            ApprovalDecision(
                generation=approval.generation,
                payload_hash=approval.payload_hash,
                decision="approve",
            ),
            str(uuid4()),
        )
        return approval, order

    return create


@pytest.fixture
def merchant_app_factory(merchant_database):
    from aftercare.config import MerchantSettings

    def create(**kwargs):
        from mock_merchant.api import create_app

        return create_app(
            MerchantSettings(
                "merchant", database_url("TEST_MERCHANT_DATABASE_URL"), "test-merchant-token"
            ),
            database=merchant_database,
            **kwargs,
        )

    return create


@pytest_asyncio.fixture
async def merchant_client(merchant_app_factory):
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=merchant_app_factory()),
        base_url="http://merchant",
        headers={"Authorization": "Bearer test-merchant-token"},
    ) as client:
        yield client


@pytest.fixture
def app_actor():
    from aftercare.contracts import Actor

    return Actor(f"test-customer-{uuid4()}", "client")


@pytest_asyncio.fixture
async def app_database(app_actor):
    from sqlalchemy import Text, delete, or_, select

    from aftercare.db import validate_test_database_url
    from aftercare.models import Approval, CommandRequest, Event, RefundOperation, Step, Task

    url = database_url("TEST_APP_DATABASE_URL")
    value = os.environ.get("TEST_MIGRATION_DATABASE_URL")
    if not value:
        pytest.fail("提交事务测试需要 TEST_MIGRATION_DATABASE_URL")
    admin_url, business = validate_test_database_url(value), make_url(url)
    if admin_url.username != "postgres" or (admin_url.host, admin_url.port, admin_url.database) != (
        business.host,
        business.port,
        business.database,
    ):
        pytest.fail("管理员必须连接同一显式 _test 数据库")
    database, admin = create_database(url), create_database(value)
    try:
        yield database
    finally:
        try:
            if os.environ.get("AFTERCARE_KEEP_DEMO") != "1":
                async with admin.sessions.begin() as session:
                    ids = select(Task.id).where(Task.customer_id == app_actor.subject)
                    await session.execute(
                        delete(CommandRequest).where(
                            or_(
                                CommandRequest.actor_id == app_actor.subject,
                                CommandRequest.response_json["task_id"].astext.in_(
                                    select(Task.id.cast(Text)).where(
                                        Task.customer_id == app_actor.subject
                                    )
                                ),
                            )
                        )
                    )
                    for model in (RefundOperation, Approval, Step, Event):
                        await session.execute(delete(model).where(model.task_id.in_(ids)))
                    await session.execute(delete(Task).where(Task.customer_id == app_actor.subject))
                    await session.execute(
                        delete(CommandRequest).where(CommandRequest.actor_id == app_actor.subject)
                    )
        finally:
            await database.engine.dispose()
            await admin.engine.dispose()


@pytest.fixture
def app_factory(app_database, app_actor):
    from aftercare.config import ApiSettings
    from aftercare.contracts import Actor

    def create():
        from aftercare.api import create_app

        return create_app(
            ApiSettings(
                "api", database_url("TEST_APP_DATABASE_URL"), "test-client", "test-reviewer"
            ),
            database=app_database,
            actors={"client": app_actor, "reviewer": Actor("reviewer_demo", "reviewer")},
        )

    return create


@pytest_asyncio.fixture
async def client(app_factory):
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_factory()),
        base_url="http://app",
        headers={"Authorization": "Bearer test-client"},
    ) as client:
        yield client


@pytest_asyncio.fixture
async def reviewer(app_factory):
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_factory()),
        base_url="http://app",
        headers={"Authorization": "Bearer test-reviewer"},
    ) as client:
        yield client


@pytest.fixture
def create_task(app_database, app_actor):
    async def create(**overrides):
        from aftercare.contracts import CreateTask
        from aftercare.tasks import create_task

        return await create_task(
            app_database,
            app_actor,
            CreateTask(
                **{"order_id": f"ORD-{uuid4()}", "message": "物流延误，申请退款", **overrides}
            ),
            str(uuid4()),
        )

    return create
