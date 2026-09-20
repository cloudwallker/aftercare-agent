"""API、商家和标准 Worker 三个真实进程的正常及丢响应演示。"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_events_tcp import api_process
from test_merchant_http import merchant_process

from mock_merchant.models import RefundRequest


@pytest.mark.parametrize("fault", [False, True], ids=["happy-path", "lost-response"])
async def test_demo(app_actor, app_database, seed_order, merchant_database, tmp_path, fault):
    order = await seed_order(customer_id=app_actor.subject)
    async with merchant_process(fault=fault) as merchant, api_process(app_actor.subject) as client:
        env = {
            k: os.environ[k]
            for k in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if k in os.environ
        }
        env.update(
            APP_ROLE="worker",
            APP_DATABASE_URL=os.environ["TEST_APP_DATABASE_URL"],
            MERCHANT_BASE_URL=str(merchant.base_url),
            MERCHANT_TOKEN="test-merchant-token",
            MODEL_MODE="mock",
        )
        with (tmp_path / "worker.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "aftercare.worker"],
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            try:
                response = await client.post(
                    "/v1/tasks",
                    json={"order_id": order.id, "message": "申请退款"},
                    headers={"Idempotency-Key": str(uuid4())},
                )
                assert response.status_code == 201
                task_id = response.json()["task_id"]

                async def until(expected):
                    async with asyncio.timeout(25):
                        while True:
                            assert process.poll() is None, "标准 Worker 提前退出"
                            task = (await client.get(f"/v1/tasks/{task_id}")).json()
                            if task["status"] == expected:
                                return task
                            assert task["status"] in {"queued", "running"}, task
                            await asyncio.sleep(0.1)

                task = await until("waiting_approval")
                approval = task["approval"]
                response = await client.post(
                    f"/v1/approvals/{approval['id']}/decision",
                    json={
                        "generation": 1,
                        "payload_hash": approval["payload_hash"],
                        "decision": "approve",
                    },
                    headers={
                        "Authorization": "Bearer tcp-reviewer",
                        "Idempotency-Key": str(uuid4()),
                    },
                )
                assert response.status_code == 200
                task = await until("succeeded")
                async with merchant_database.sessions() as session:
                    rows = (
                        await session.scalars(
                            select(RefundRequest).where(RefundRequest.order_id == order.id)
                        )
                    ).all()
                    assert len(rows) == 1 and rows[0].status == "confirmed"
                    assert str(rows[0].refund_id) == task["result"]["refund_id"]
                assert task["operation"]["dispatch_count"] == 1
                assert task["operation"]["reconcile_count"] == (2 if fault else 1)
                events = await client.get(f"/v1/tasks/{task_id}/events")
                assert (
                    "event: task.completed" in events.text
                    and "event: refund.confirmed" in events.text
                )
                print(
                    json.dumps(
                        {
                            "scenario": "lost-response" if fault else "happy-path",
                            "task_id": task_id,
                            "approval_id": approval["id"],
                            "operation_key": task["operation"]["operation_key"],
                            "refund_id": task["result"]["refund_id"],
                            "confirmed_count": len(rows),
                        },
                        ensure_ascii=False,
                    )
                )
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        await asyncio.to_thread(process.wait, timeout=12)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait, timeout=5)
