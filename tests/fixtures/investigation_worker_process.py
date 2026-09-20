"""调查强杀测试组合根：在已提交工具结果后发信号并暂停。"""

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

import httpx

from aftercare.agent import runner
from aftercare.agent.mock_provider import MockProvider
from aftercare.db import create_database
from aftercare.models import Task
from aftercare.refunds.merchant_client import MerchantClient
from aftercare.worker import Worker


async def main():
    task_id, stage, signal = UUID(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    database = create_database(os.environ["APP_DATABASE_URL"])
    original = runner.complete_step

    async def completion(*args, **kwargs):
        await original(*args, **kwargs)
        if args[2].name == "get_order" and stage == "pause":
            signal.write_text(str(os.getpid()), encoding="utf-8")
            await asyncio.Event().wait()

    runner.complete_step = completion
    try:
        async with httpx.AsyncClient(
            base_url=os.environ["MERCHANT_BASE_URL"],
            headers={"Authorization": "Bearer test-merchant-token"},
        ) as client:

            async def investigate(lease):
                assert lease.task_id == task_id
                await runner.run_investigation(
                    database, lease, MockProvider(), MerchantClient(client)
                )

            worker = Worker(
                database,
                f"investigate-{os.getpid()}",
                handlers={"investigate": investigate},
                concurrency=1,
                lease_seconds=2,
                heartbeat_seconds=0.4,
            )
            async with asyncio.timeout(20):
                while not await worker.once():
                    await asyncio.sleep(0.05)
            async with database.sessions() as session:
                assert (await session.get(Task, task_id)).status == "waiting_approval"
    finally:
        await database.engine.dispose()


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
asyncio.run(main())
