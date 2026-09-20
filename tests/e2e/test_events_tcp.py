"""真实 TCP 流验证十五秒心跳、断连、Last-Event-ID 与终态关闭。"""

import asyncio
import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from sqlalchemy import select

from aftercare.events import append_event
from aftercare.models import Task


@asynccontextmanager
async def api_process(customer):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if k in os.environ
    }
    env.update(
        APP_ROLE="api",
        APP_DATABASE_URL=os.environ["TEST_APP_DATABASE_URL"],
        CLIENT_TOKEN="tcp-client",
        REVIEWER_TOKEN="tcp-reviewer",
        TEST_CUSTOMER=customer,
    )
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "api_test_app:create_app",
            "--factory",
            "--loop",
            "asyncio:SelectorEventLoop",
            "--app-dir",
            str(root / "tests/fixtures"),
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
            timeout=20,
            headers={"Authorization": "Bearer tcp-client"},
        ) as client:
            for _ in range(200):
                assert proc.poll() is None
                try:
                    if (await client.get("/health/ready")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("API 未就绪")
            yield client
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.to_thread(proc.wait, timeout=5)


async def test_real_heartbeat_disconnect_resume_and_final(app_database, app_actor, create_task):
    created = await create_task()
    path = f"/v1/tasks/{created.task_id}/events"
    async with api_process(app_actor.subject) as client:
        async with asyncio.timeout(22):
            async with client.stream("GET", path) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                seen = []
                async for line in response.aiter_lines():
                    seen.append(line)
                    if line == ": heartbeat":
                        break
                assert "id: 1" in seen
        # 连接已断开，状态更新不被流事务锁阻塞。
        async with app_database.sessions.begin() as session:
            task = await session.scalar(
                select(Task).where(Task.id == created.task_id).with_for_update()
            )
            task.status = "succeeded"
            await append_event(session, task, "task.completed", {})
        async with asyncio.timeout(5):
            async with client.stream("GET", path, headers={"Last-Event-ID": "1"}) as response:
                lines = [line async for line in response.aiter_lines()]
        assert "id: 2" in lines and "id: 1" not in lines and "event: task.completed" in lines
        async with asyncio.timeout(5):
            response = await client.get(path, headers={"Last-Event-ID": "2"})
        assert response.status_code == 200 and response.text == ""
        assert (await client.get("/health/ready")).status_code == 200
