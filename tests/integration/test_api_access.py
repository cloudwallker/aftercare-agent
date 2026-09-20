"""新增查询与事件接口的身份及游标校验。"""

from uuid import uuid4

import pytest


@pytest.mark.parametrize(
    "value,status", [("-1", 422), ("abc", 422), ("1.5", 422), ("999", 409), ("9" * 40, 422)]
)
async def test_event_cursor_validation(client, create_task, value, status):
    created = await create_task()
    response = await client.get(
        f"/v1/tasks/{created.task_id}/events", headers={"Last-Event-ID": value}
    )
    assert response.status_code == status and response.json()["error"]["request_id"]


@pytest.mark.parametrize("suffix", ["", "/steps", "/approvals", "/events"])
async def test_unknown_task_not_visible(client, suffix):
    response = await client.get(f"/v1/tasks/{uuid4()}{suffix}")
    assert response.status_code == 404


async def test_missing_auth_and_reviewer_create(client, reviewer):
    response = await client.get("/v1/tasks", headers={"Authorization": ""})
    assert response.status_code == 401
    response = await reviewer.post(
        "/v1/tasks",
        json={"order_id": "O", "message": "退款"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 403


@pytest.mark.parametrize("suffix", ["", "/steps", "/approvals", "/events"])
async def test_other_customer_cannot_read(client, create_task, monkeypatch, suffix):
    from aftercare import api
    from aftercare.contracts import Actor

    created = await create_task()
    monkeypatch.setattr(api, "authenticate", lambda *args: Actor("other-customer", "client"))
    response = await client.get(f"/v1/tasks/{created.task_id}{suffix}")
    assert response.status_code == 404
