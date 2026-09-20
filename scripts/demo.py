"""隔离的可重复故障演示；inspect-refund 只查询，不修订任务或退款。"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID


async def inspect(task_id):
    import httpx

    from aftercare.config import WorkerSettings, load_settings
    from aftercare.db import create_database
    from aftercare.refunds.inspection import inspect_refund
    from aftercare.refunds.merchant_client import RefundMerchantClient

    settings = load_settings()
    if not isinstance(settings, WorkerSettings):
        raise ValueError("只读核查须在 Worker 配置中执行")
    database = create_database(settings.database_url)
    try:
        async with httpx.AsyncClient(
            base_url=settings.merchant_base_url,
            headers={"Authorization": f"Bearer {settings.merchant_token}"},
        ) as client:
            result = await inspect_refund(database, RefundMerchantClient(client), task_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await database.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        required=True,
        choices=["happy-path", "lost-response", "worker-restart", "inspect-refund"],
    )
    parser.add_argument("--task-id", type=UUID)
    parser.add_argument("--project")
    parser.add_argument("--test", action="store_true", help="只读核查使用 compose.test.yaml")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.scenario == "inspect-refund":
        if not args.task_id:
            parser.error("inspect-refund 必须传 --task-id")
        if args.inside:
            asyncio.run(inspect(args.task_id))
            return
        command = ["docker", "compose", "-p", args.project or "aftercare", "-f", "compose.yaml"]
        if args.test:
            command += ["-f", "compose.test.yaml"]
        raise SystemExit(
            subprocess.call(
                command
                + [
                    "exec",
                    "-T",
                    "worker",
                    "uv",
                    "run",
                    "--no-sync",
                    "python",
                    "scripts/demo.py",
                    "--scenario",
                    "inspect-refund",
                    "--task-id",
                    str(args.task_id),
                    "--inside",
                ],
                cwd=root,
            )
        )
    if args.inside:
        from aftercare.db import validate_test_database_url

        for name in (
            "TEST_APP_DATABASE_URL",
            "TEST_MERCHANT_DATABASE_URL",
            "TEST_MIGRATION_DATABASE_URL",
        ):
            validate_test_database_url(os.environ[name])
        test = (
            "tests/e2e/test_refund_crash_windows.py"
            if args.scenario == "worker-restart"
            else f"tests/e2e/test_demo.py::test_demo[{args.scenario}]"
        )
        raise SystemExit(
            subprocess.call([sys.executable, "-m", "pytest", test, "-q", "-s"], cwd=root)
        )
    project = args.project or "aftercare-demo"
    if not project.endswith("-demo"):
        parser.error("故障演示 project 必须以 -demo 结尾，与开发服务隔离")
    command = ["docker", "compose", "-p", project, "-f", "compose.yaml", "-f", "compose.test.yaml"]
    try:
        result = subprocess.call(
            command
            + [
                "run",
                "--rm",
                "--build",
                "-e",
                "AFTERCARE_KEEP_DEMO=1",
                "tests",
                "uv",
                "run",
                "--no-sync",
                "python",
                "scripts/demo.py",
                "--inside",
                "--scenario",
                args.scenario,
            ],
            cwd=root,
        )
    finally:
        subprocess.call(command + ["down"], cwd=root)
    raise SystemExit(result)


if __name__ == "__main__":
    main()
