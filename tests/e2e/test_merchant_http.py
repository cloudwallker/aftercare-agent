"""通过真实 Uvicorn TCP 服务验证退款提交后丢失响应与商家进程重启。"""

import asyncio
import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import select

from mock_merchant.models import Order, RefundRequest


@asynccontextmanager
async def merchant_process(*, fault=True):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # 子进程只接收商家凭据；测试管理员连接不传播到 HTTP 进程。
    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if key in os.environ
    }
    env.update(
        {
            "APP_ROLE": "merchant",
            "MERCHANT_DATABASE_URL": os.environ["TEST_MERCHANT_DATABASE_URL"],
            "MERCHANT_TOKEN": "test-merchant-token",
            "PYTHONUNBUFFERED": "1",
        }
    )
    root = Path(__file__).resolve().parents[2]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "merchant_fault_app:create_app" if fault else "mock_merchant.api:create_app",
            "--factory",
            "--loop",
            "asyncio:SelectorEventLoop",
            "--app-dir",
            str(root / "tests" / "fixtures"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            timeout=3,
            headers={"Authorization": "Bearer test-merchant-token"},
        ) as client:
            deadline = asyncio.get_running_loop().time() + 15
            while asyncio.get_running_loop().time() < deadline:
                if process.poll() is not None:
                    raise AssertionError(f"测试商家进程提前退出，exit={process.returncode}")
                try:
                    if (await client.get("/health/ready")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("测试商家进程 15 秒内未就绪")
            yield client
    finally:
        # 仅回收本测试 Popen 创建并持有的进程。
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)


async def test_committed_refund_survives_merchant_process_restart(seed_order, merchant_database):
    order = await seed_order()
    key = str(uuid4())
    payload = {
        "order_id": order.id,
        "customer_id": order.customer_id,
        "amount_cents": 19900,
        "currency": "CNY",
        "expected_order_version": 1,
        "policy_version": "refund_v1",
        "authorization_id": str(uuid4()),
    }
    async with merchant_process() as client:
        lost = await client.post("/refunds", json=payload, headers={"Idempotency-Key": key})
        assert lost.status_code == 503
        confirmed = (await client.get(f"/refunds/by-key/{key}")).json()
        assert confirmed["status"] == "confirmed"
    async with merchant_process() as client:
        replay = await client.post("/refunds", json=payload, headers={"Idempotency-Key": key})
        assert replay.status_code == 200 and replay.json() == confirmed
        assert (await client.get(f"/refunds/by-key/{key}")).json() == confirmed
    async with merchant_database.sessions() as session:
        saved = await session.get(Order, order.id)
        rows = (
            await session.scalars(select(RefundRequest).where(RefundRequest.order_id == order.id))
        ).all()
        assert saved.refunded and saved.order_version == 2
        assert len(rows) == 1 and rows[0].fault_consumed
        assert str(rows[0].refund_id) == confirmed["refund_id"]
