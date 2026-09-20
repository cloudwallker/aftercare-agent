"""阻止进程误用管理员连接、配置缺失或泄漏其他角色的密钥。"""

import pytest


def load(env):
    from aftercare.config import load_settings

    return load_settings(env)


def test_api_only_needs_its_own_configuration():
    settings = load(
        {
            "APP_ROLE": "api",
            "APP_DATABASE_URL": "postgresql+psycopg://aftercare_app:demo@localhost/aftercare",
            "CLIENT_TOKEN": "client-demo",
            "REVIEWER_TOKEN": "reviewer-demo",
            "MIGRATION_DATABASE_URL": "postgresql+psycopg://postgres:admin@localhost/aftercare",
            "MODEL_API_KEY": "unrelated-secret",
        }
    )
    assert settings.role == "api"
    assert "unrelated-secret" not in repr(settings)
    assert "admin" not in repr(settings)
    assert not hasattr(settings, "model_api_key")
    assert not hasattr(settings, "migration_database_url")


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///local.db",
        "postgresql+psycopg://postgres:demo@localhost/aftercare",
        "postgresql+psycopg://aftercare_merchant:demo@localhost/aftercare",
    ],
)
def test_api_rejects_wrong_database_role_or_driver(url):
    with pytest.raises(ValueError):
        load(
            {
                "APP_ROLE": "api",
                "APP_DATABASE_URL": url,
                "CLIENT_TOKEN": "client",
                "REVIEWER_TOKEN": "reviewer",
            }
        )


@pytest.mark.parametrize("reviewer_token", ["same", " same "])
def test_api_rejects_shared_tokens(reviewer_token):
    with pytest.raises(ValueError, match="TOKEN"):
        load(
            {
                "APP_ROLE": "api",
                "APP_DATABASE_URL": "postgresql+psycopg://aftercare_app:p@localhost/aftercare",
                "CLIENT_TOKEN": "same",
                "REVIEWER_TOKEN": reviewer_token,
            }
        )


def test_real_model_requires_all_credentials():
    with pytest.raises(ValueError, match="MODEL_BASE_URL"):
        load(
            {
                "APP_ROLE": "worker",
                "APP_DATABASE_URL": "postgresql+psycopg://aftercare_app:p@localhost/aftercare",
                "MERCHANT_TOKEN": "merchant-demo",
                "MERCHANT_BASE_URL": "http://merchant:8001",
                "MODEL_MODE": "http",
            }
        )


def test_heartbeat_must_fit_inside_lease():
    with pytest.raises(ValueError, match="HEARTBEAT_SECONDS"):
        load(
            {
                "APP_ROLE": "worker",
                "APP_DATABASE_URL": "postgresql+psycopg://aftercare_app:p@localhost/aftercare",
                "MERCHANT_TOKEN": "merchant-demo",
                "MERCHANT_BASE_URL": "http://merchant:8001",
                "LEASE_SECONDS": "5",
                "HEARTBEAT_SECONDS": "5",
            }
        )


@pytest.mark.parametrize("role", ["migrate", "seed", "merchant"])
def test_service_role_does_not_require_api_tokens(role):
    env = {"APP_ROLE": role}
    if role == "migrate":
        env["MIGRATION_DATABASE_URL"] = "postgresql+psycopg://postgres:p@localhost/aftercare"
    else:
        env["MERCHANT_DATABASE_URL"] = (
            "postgresql+psycopg://aftercare_merchant:p@localhost/aftercare"
        )
    if role == "merchant":
        env["MERCHANT_TOKEN"] = "merchant-demo"
    assert load(env).role == role


def test_database_url_is_hidden_in_repr():
    settings = load(
        {
            "APP_ROLE": "seed",
            "MERCHANT_DATABASE_URL": "postgresql+psycopg://aftercare_merchant:secret@localhost/aftercare",
        }
    )
    assert "secret" not in repr(settings)


def test_test_database_guard_rejects_development_database():
    from aftercare.db import validate_test_database_url

    with pytest.raises(ValueError, match="_test"):
        validate_test_database_url("postgresql+psycopg://aftercare_app:p@localhost/aftercare")
    assert (
        validate_test_database_url(
            "postgresql+psycopg://aftercare_app:p@localhost/aftercare_test"
        ).database
        == "aftercare_test"
    )
