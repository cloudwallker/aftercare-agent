"""仅强杀本测试创建的 Worker，真实 HTTP 和租约等待验证两个崩溃窗口。"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from test_merchant_http import merchant_process

from aftercare.models import RefundOperation, Task
from mock_merchant.models import RefundRequest


@pytest.mark.parametrize("stage", ["before_refund_http", "after_refund_http_before_commit"])
async def test_force_kill_then_reconcile(
    approved_refund, app_database, merchant_database, tmp_path, stage
):
    approval, order = await approved_refund()
    root = Path(__file__).resolve().parents[2]
    signal = tmp_path / "hook.json"
    children = []
    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if key in os.environ
    }
    env["APP_DATABASE_URL"] = os.environ["TEST_APP_DATABASE_URL"]

    async with merchant_process(fault=False) as client:
        env["MERCHANT_BASE_URL"] = str(client.base_url)
        with (tmp_path / "worker.log").open("wb") as log:

            def start(point):
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(root / "tests/fixtures/refund_worker_process.py"),
                        str(approval.task_id),
                        point,
                        str(signal),
                    ],
                    cwd=root,
                    env=env,
                    stdout=log,
                    stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                children.append(process)
                return process

            try:
                first = start(stage)
                deadline = asyncio.get_running_loop().time() + 20
                while not signal.exists() and asyncio.get_running_loop().time() < deadline:
                    assert first.poll() is None, "测试 Worker 提前退出，详情见 worker.log"
                    await asyncio.sleep(0.05)
                assert signal.exists(), "未抵达指定 Hook"
                info = json.loads(signal.read_text(encoding="utf-8"))
                assert info["pid"] == first.pid and info["task_id"] == str(approval.task_id)
                async with app_database.sessions() as session:
                    operation = await session.scalar(
                        select(RefundOperation).where(RefundOperation.task_id == approval.task_id)
                    )
                    original_key = operation.operation_key
                    assert operation.status == "unknown" and operation.dispatch_count == 1
                first.kill()
                await asyncio.to_thread(first.wait, timeout=5)
                recovery_started = asyncio.get_running_loop().time()
                second = start("none")
                await asyncio.to_thread(second.wait, timeout=20)
                assert second.returncode == 0, (tmp_path / "worker.log").read_text(encoding="utf-8")
                async with app_database.sessions() as session:
                    task = await session.get(Task, approval.task_id)
                    operation = await session.scalar(
                        select(RefundOperation).where(RefundOperation.task_id == approval.task_id)
                    )
                    assert task.status == "succeeded" and task.result["outcome"] == "refunded"
                    assert (
                        operation.operation_key == original_key and operation.status == "confirmed"
                    )
                    assert operation.dispatch_count == (2 if stage == "before_refund_http" else 1)
                    assert task.lease_epoch == 2
                async with merchant_database.sessions() as session:
                    rows = (
                        await session.scalars(
                            select(RefundRequest).where(
                                RefundRequest.order_id == order.id,
                                RefundRequest.status == "confirmed",
                            )
                        )
                    ).all()
                    assert len(rows) == 1 and rows[0].refund_id == operation.merchant_refund_id
                print(
                    json.dumps(
                        {
                            "scenario": "worker-restart",
                            "window": stage,
                            "task_id": str(approval.task_id),
                            "operation_key": original_key,
                            "refund_id": str(operation.merchant_refund_id),
                            "confirmed_count": len(rows),
                            "recovery_seconds": round(
                                asyncio.get_running_loop().time() - recovery_started, 3
                            ),
                        }
                    )
                )
            finally:
                for process in children:
                    if process.poll() is None:
                        process.kill()
                        await asyncio.to_thread(process.wait, timeout=5)
