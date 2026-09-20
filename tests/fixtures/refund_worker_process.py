"""专用测试 Worker；故障 Hook 只由本测试入口注入。"""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

import httpx

from aftercare.db import create_database
from aftercare.models import Task
from aftercare.refunds.executor import execute_refund
from aftercare.refunds.merchant_client import RefundMerchantClient
from aftercare.worker import Worker


async def main():
    task_id, stage, signal_path = UUID(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    database = create_database(os.environ["APP_DATABASE_URL"])
    try:
        async with httpx.AsyncClient(
            base_url=os.environ["MERCHANT_BASE_URL"],
            headers={"Authorization": "Bearer test-merchant-token"},
        ) as client:

            async def hook(name, lease):
                if name == stage:
                    signal_path.write_text(
                        json.dumps(
                            {"pid": os.getpid(), "stage": name, "task_id": str(lease.task_id)}
                        ),
                        encoding="utf-8",
                    )
                    await asyncio.Event().wait()

            async def refund(lease):
                assert lease.task_id == task_id
                await execute_refund(database, lease, RefundMerchantClient(client), hook=hook)

            async def investigate(lease):
                raise AssertionError("本测试只运行指定退款任务")

            worker = Worker(
                database,
                f"crash-worker-{os.getpid()}",
                handlers={"investigate": investigate, "execute_refund": refund},
                concurrency=1,
                lease_seconds=2,
                heartbeat_seconds=0.4,
            )
            deadline = asyncio.get_running_loop().time() + 15
            while asyncio.get_running_loop().time() < deadline:
                if await worker.once():
                    async with database.sessions() as session:
                        task = await session.get(Task, task_id)
                        assert task.status == "succeeded", (task.status, task.error_code)
                    return
                await asyncio.sleep(0.05)
            raise AssertionError("未能在期限内接管任务")
    finally:
        await database.engine.dispose()


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
asyncio.run(main())
