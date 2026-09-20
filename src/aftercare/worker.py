"""独立 Worker 调度原语；业务 handler 在后续任务从组合根注入。"""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

from aftercare.contracts import Lease
from aftercare.db import Database
from aftercare.runtime.leases import (
    LeaseLost,
    claim_next,
    fail_task,
    locked_task,
    renew_lease,
)
from aftercare.runtime.steps import BudgetExceeded, PayloadTooLarge

logger = logging.getLogger(__name__)
Handler = Callable[[Lease], Awaitable[None]]


class Worker:
    def __init__(
        self,
        database: Database,
        worker_id: str,
        *,
        handlers: dict[str, Handler],
        concurrency: int = 4,
        lease_seconds: int = 20,
        heartbeat_seconds: float = 5,
        poll_seconds: float = 1,
        shutdown_seconds: float = 10,
    ):
        if not handlers.get("investigate") or set(handlers) - {"investigate", "execute_refund"}:
            raise ValueError("必须注入 investigate handler；尚未接入 Agent 时不能启动 Worker")
        if (
            not worker_id
            or type(concurrency) is not int
            or concurrency < 1
            or not 0 < heartbeat_seconds < lease_seconds
            or poll_seconds <= 0
            or not 0 < shutdown_seconds <= 10
        ):
            raise ValueError("Worker 并发、租约或关闭配置无效")
        self.database, self.worker_id = database, worker_id
        self.handlers = dict(handlers)
        self.concurrency = concurrency
        self.lease_seconds, self.heartbeat_seconds = lease_seconds, heartbeat_seconds
        self.poll_seconds, self.shutdown_seconds = poll_seconds, shutdown_seconds
        self._slots = asyncio.Semaphore(concurrency)

    async def _heartbeat(self, lease: Lease):
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if not await renew_lease(self.database, lease, self.lease_seconds):
                return

    async def _dispatch(self, lease: Lease):
        async with self.database.sessions.begin() as session:
            task, _ = await locked_task(session, lease)
            phase = task.phase
        if phase not in self.handlers:
            raise ValueError("phase handler 未配置")
        # handler 调用期间没有持有数据库事务。每次写回需重新 fencing。
        await self.handlers[phase](lease)

    async def once(self) -> bool:
        async with self._slots:
            lease = await claim_next(self.database, self.worker_id, self.lease_seconds)
            if lease is None:
                return False
            work = asyncio.create_task(self._dispatch(lease))
            heartbeat = asyncio.create_task(self._heartbeat(lease))
            try:
                done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
                if work not in done:
                    # 心跳失败（含 DB 不可用）时停止外部处理，保留状态等待接管。
                    work.cancel()
                    await asyncio.gather(work, heartbeat, return_exceptions=True)
                    return True
                try:
                    await work
                    await fail_task(self.database, lease, "HANDLER_INCOMPLETE")
                except LeaseLost:
                    pass
                except Exception as exc:
                    code = (
                        exc.code
                        if isinstance(exc, (BudgetExceeded, PayloadTooLarge))
                        else "WORKER_ERROR"
                    )
                    logger.error(
                        "worker handler failed task_id=%s error_code=%s", lease.task_id, code
                    )
                    try:
                        await fail_task(self.database, lease, code)
                    except LeaseLost:
                        pass
                return True
            finally:
                work.cancel()
                heartbeat.cancel()
                await asyncio.gather(work, heartbeat, return_exceptions=True)

    async def run(self, stop: asyncio.Event):
        async def expire():
            from aftercare.approvals import expire_approvals

            while not stop.is_set():
                await expire_approvals(self.database)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass

        async def consume():
            while not stop.is_set():
                found = await self.once()
                if not found:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
                    except TimeoutError:
                        pass

        runners = [asyncio.create_task(consume()) for _ in range(self.concurrency)]
        runners.append(asyncio.create_task(expire()))
        stopping = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait([*runners, stopping], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not stopping:
                    await task
        finally:
            stop.set()
            stopping.cancel()
            _, pending = await asyncio.wait(runners, timeout=self.shutdown_seconds)
            for task in pending:
                task.cancel()
            await asyncio.gather(*runners, stopping, return_exceptions=True)


async def serve(worker: Worker):
    """SIGTERM/SIGINT 停止领取；最多等待 10 秒，然后停止心跳交给租约恢复。"""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set()))
    try:
        await worker.run(stop)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import socket
    from contextlib import AsyncExitStack
    from uuid import uuid4

    import httpx

    from aftercare.agent.mock_provider import MockProvider
    from aftercare.agent.runner import run_investigation
    from aftercare.config import WorkerSettings, load_settings
    from aftercare.db import create_database
    from aftercare.refunds.executor import execute_refund
    from aftercare.refunds.merchant_client import MerchantClient, RefundMerchantClient

    settings = load_settings()
    if not isinstance(settings, WorkerSettings):
        raise ValueError("Worker 入口需要 APP_ROLE=worker")
    database = create_database(settings.database_url)
    try:
        async with (
            httpx.AsyncClient(
                base_url=settings.merchant_base_url,
                headers={"Authorization": f"Bearer {settings.merchant_token}"},
                follow_redirects=False,
            ) as client,
            AsyncExitStack() as resources,
        ):
            merchant = MerchantClient(client)
            if settings.model_mode == "mock":
                provider = MockProvider()
            else:
                from aftercare.agent.http_provider import HttpProvider

                model_client = await resources.enter_async_context(
                    httpx.AsyncClient(
                        base_url=settings.model_base_url + "/",
                        headers={"Authorization": f"Bearer {settings.model_api_key}"},
                        follow_redirects=False,
                    )
                )
                provider = HttpProvider(model_client, settings.model_name)

            async def investigate(lease):
                await run_investigation(database, lease, provider, merchant)

            async def refund(lease):
                await execute_refund(database, lease, RefundMerchantClient(client))

            await serve(
                Worker(
                    database,
                    f"{socket.gethostname()}-{uuid4()}",
                    handlers={"investigate": investigate, "execute_refund": refund},
                    concurrency=settings.worker_concurrency,
                    lease_seconds=settings.lease_seconds,
                    heartbeat_seconds=settings.heartbeat_seconds,
                )
            )
    finally:
        await database.engine.dispose()


if __name__ == "__main__":
    import sys

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
