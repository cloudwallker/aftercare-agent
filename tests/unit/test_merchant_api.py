"""验证商家故障入口和健康检查的部署边界。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from aftercare.config import MerchantSettings


def settings(role="merchant"):
    return MerchantSettings(
        role, "postgresql+psycopg://aftercare_merchant:x@localhost/test_test", "secret"
    )


def test_fault_injection_requires_testing():
    from mock_merchant.api import create_app

    with pytest.raises(ValueError, match="testing"):
        create_app(settings(), fault_mode="commit_then_503_once")
    with pytest.raises(ValueError, match="fault_mode"):
        create_app(settings(), testing=True, fault_mode="unknown")


def test_merchant_app_rejects_other_process_role():
    from mock_merchant.api import create_app

    with pytest.raises(ValueError, match="merchant"):
        create_app(settings("worker"))


@pytest.mark.asyncio
async def test_readiness_errors_are_sanitized():
    from mock_merchant.api import create_app

    class UnavailableDatabase:
        def sessions(self):
            raise RuntimeError("postgresql://user:secret@private-host/internal")

    app = create_app(settings(), database=UnavailableDatabase())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://merchant"
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert "secret" not in response.text and "private-host" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("versions", [[], ["old_revision"], ["0001_initial", "other_branch"]])
async def test_readiness_rejects_missing_or_incorrect_migration(versions):
    from mock_merchant.api import create_app

    class UnmigratedDatabase:
        @asynccontextmanager
        async def sessions(self):
            yield self

        async def scalars(self, statement):
            return SimpleNamespace(all=lambda: versions)

        async def execute(self, statement):
            return None

    app = create_app(settings(), database=UnmigratedDatabase())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://merchant"
    ) as client:
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready"}
