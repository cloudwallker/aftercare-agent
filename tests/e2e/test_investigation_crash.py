"""成功工具结果后的真实强杀不能重新查询或重置本代预算。"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select
from test_merchant_http import merchant_process

from aftercare.models import Step, Task


async def test_committed_tool_survives_force_kill(
    app_database, app_actor, create_task, seed_order, tmp_path
):
    order = await seed_order(customer_id=app_actor.subject)
    created = await create_task(order_id=order.id)
    signal = tmp_path / "saved"
    root = Path(__file__).resolve().parents[2]
    children = []
    async with merchant_process(fault=False) as merchant:
        env = {
            k: os.environ[k]
            for k in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if k in os.environ
        }
        env.update(
            APP_DATABASE_URL=os.environ["TEST_APP_DATABASE_URL"],
            MERCHANT_BASE_URL=str(merchant.base_url),
        )
        with (tmp_path / "worker.log").open("wb") as log:

            def start(stage):
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(root / "tests/fixtures/investigation_worker_process.py"),
                        str(created.task_id),
                        stage,
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
                first = start("pause")
                async with asyncio.timeout(15):
                    while not signal.exists():
                        assert first.poll() is None
                        await asyncio.sleep(0.05)
                assert int(signal.read_text()) == first.pid
                async with app_database.sessions() as session:
                    task = await session.get(Task, created.task_id)
                    started = task.generation_started_at
                    assert task.tool_call_count == 1
                first.kill()
                await asyncio.to_thread(first.wait, timeout=5)
                second = start("resume")
                await asyncio.to_thread(second.wait, timeout=25)
                assert second.returncode == 0, (tmp_path / "worker.log").read_text()
                async with app_database.sessions() as session:
                    task = await session.get(Task, created.task_id)
                    steps = (
                        await session.scalars(
                            select(Step).where(Step.task_id == task.id, Step.name == "get_order")
                        )
                    ).all()
                    assert (
                        task.status == "waiting_approval" and task.generation_started_at == started
                    )
                    assert task.model_call_count == 4 and task.tool_call_count == 3
                    assert len(steps) == 1 and steps[0].attempt_count == 1 and task.lease_epoch == 2
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        await asyncio.to_thread(child.wait, timeout=5)
