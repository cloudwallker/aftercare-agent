"""在隔离测试库运行固定二十条协议评测，输出 Mock/HTTP 分开的证据报告。"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


async def evaluate(mode, output):
    import httpx
    from sqlalchemy import select

    from aftercare.agent.http_provider import HttpProvider
    from aftercare.agent.mock_provider import MockProvider
    from aftercare.agent.runner import run_investigation
    from aftercare.approvals import decide_approval
    from aftercare.config import load_settings
    from aftercare.contracts import Actor, ApprovalDecision, CreateTask
    from aftercare.db import create_database, validate_test_database_url
    from aftercare.models import Approval, Task
    from aftercare.refunds.executor import execute_refund
    from aftercare.refunds.merchant_client import MerchantClient, RefundMerchantClient
    from aftercare.tasks import create_task
    from aftercare.worker import Worker
    from mock_merchant.models import RefundRequest
    from mock_merchant.seed import seed_orders

    sys.path.insert(0, str(ROOT / "tests/e2e"))
    from test_merchant_http import merchant_process

    app_url, merchant_url = (
        os.environ["TEST_APP_DATABASE_URL"],
        os.environ["TEST_MERCHANT_DATABASE_URL"],
    )
    validate_test_database_url(app_url)
    validate_test_database_url(merchant_url)
    cases = [
        json.loads(line)
        for line in (ROOT / "tests/fixtures/tickets.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(cases) == 20 and len({case["case_id"] for case in cases}) == 20
    database, merchant_db = create_database(app_url), create_database(merchant_url)
    run_id, results = uuid4().hex[:12], []
    started = time.monotonic()
    try:
        async with merchant_process(fault=False) as merchant_http:
            settings = None
            if mode == "http":
                settings = load_settings(
                    {
                        "APP_ROLE": "worker",
                        "APP_DATABASE_URL": app_url,
                        "MERCHANT_BASE_URL": str(merchant_http.base_url),
                        "MERCHANT_TOKEN": "test-merchant-token",
                        "MODEL_MODE": "http",
                        **{
                            key: os.environ[key]
                            for key in ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME")
                        },
                    }
                )
            async with httpx.AsyncClient(
                base_url=(settings.model_base_url + "/") if settings else "http://unused/",
                headers={
                    "Authorization": "Bearer " + (settings.model_api_key if settings else "unused")
                },
                follow_redirects=False,
            ) as model_http:
                for case in cases:
                    prefix = f"eval-{run_id}-{case['case_id']}-"
                    await seed_orders(merchant_db, prefix=prefix)
                    order_id = prefix + case["order_fixture"]
                    async with merchant_db.sessions() as session:
                        before = len(
                            (
                                await session.scalars(
                                    select(RefundRequest).where(
                                        RefundRequest.order_id == order_id,
                                        RefundRequest.status == "confirmed",
                                    )
                                )
                            ).all()
                        )
                    provider = (
                        MockProvider()
                        if mode == "mock"
                        else HttpProvider(model_http, os.environ["MODEL_NAME"])
                    )
                    created = await create_task(
                        database,
                        Actor("customer_demo", "client"),
                        CreateTask(order_id=order_id, message=case["message"]),
                        str(uuid4()),
                    )

                    async def investigate(
                        lease, expected=created.task_id, current_provider=provider
                    ):
                        if lease.task_id != expected:
                            raise RuntimeError("评测库存在其他可运行任务")
                        await run_investigation(
                            database, lease, current_provider, MerchantClient(merchant_http)
                        )

                    async def refund(lease, expected=created.task_id):
                        if lease.task_id != expected:
                            raise RuntimeError("评测库存在其他可运行任务")
                        await execute_refund(database, lease, RefundMerchantClient(merchant_http))

                    worker = Worker(
                        database,
                        f"eval-{run_id}",
                        handlers={"investigate": investigate, "execute_refund": refund},
                        concurrency=1,
                    )
                    case_started = time.monotonic()
                    while True:
                        async with database.sessions() as session:
                            task = await session.get(Task, created.task_id)
                            approval = await session.scalar(
                                select(Approval).where(Approval.task_id == task.id)
                            )
                        if task.status in {"succeeded", "rejected", "failed", "manual_review"}:
                            break
                        if task.status == "waiting_approval":
                            await decide_approval(
                                database,
                                Actor("eval-reviewer", "reviewer"),
                                approval.id,
                                ApprovalDecision(
                                    generation=1,
                                    payload_hash=approval.payload_hash,
                                    decision="approve",
                                ),
                                str(uuid4()),
                            )
                        if not await worker.once():
                            await asyncio.sleep(0.1)
                        if time.monotonic() - case_started > 330:
                            raise TimeoutError("评测超过调查和对账总预算")
                    recommendation = next(
                        (
                            m["output"]["recommendation"]
                            for m in reversed(task.checkpoint["messages"])
                            if m["output"].get("kind") == "final"
                        ),
                        None,
                    )
                    outcome = task.result["outcome"] if task.result else task.status
                    async with merchant_db.sessions() as session:
                        count = len(
                            (
                                await session.scalars(
                                    select(RefundRequest).where(
                                        RefundRequest.order_id == order_id,
                                        RefundRequest.status == "confirmed",
                                    )
                                )
                            ).all()
                        )
                    results.append(
                        {
                            "case_id": case["case_id"],
                            "task_id": str(task.id),
                            "expected_recommendation": case["expected_recommendation"],
                            "recommendation": recommendation,
                            "expected_outcome": case["expected_outcome"],
                            "outcome": outcome,
                            "correct": outcome == case["expected_outcome"],
                            "error_code": task.error_code,
                            "model_calls": task.model_call_count,
                            "tool_calls": task.tool_call_count,
                            "tokens": provider.token_count
                            if mode == "http" and provider.usage_complete
                            else None,
                            "duration_seconds": round(time.monotonic() - case_started, 3),
                            "new_refunds": count - before,
                            "duplicate_refunds": max(0, count - max(1, before)),
                        }
                    )
                    print(f"{case['case_id']}: {outcome}", flush=True)
    finally:
        await database.engine.dispose()
        await merchant_db.engine.dispose()
    report = {
        "mode": mode,
        "model": "MockProvider" if mode == "mock" else os.environ["MODEL_NAME"],
        "version": version("aftercare-agent"),
        "dataset_sha256": hashlib.sha256(
            (ROOT / "tests/fixtures/tickets.jsonl").read_bytes()
        ).hexdigest(),
        "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "date": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "sample_count": len(results),
        "correct": sum(r["correct"] for r in results),
        "business_accuracy": sum(r["correct"] for r in results) / len(results),
        "recommendation_accuracy": sum(
            r["recommendation"] == r["expected_recommendation"] for r in results
        )
        / len(results),
        "blocked_refund_suggestions": sum(r["error_code"] == "POLICY_DENIED" for r in results),
        "duplicate_refunds": sum(r["duplicate_refunds"] for r in results),
        "model_calls": sum(r["model_calls"] for r in results),
        "tokens": sum(r["tokens"] for r in results)
        if all(r["tokens"] is not None for r in results)
        else None,
        "error_categories": dict(Counter(r["error_code"] for r in results if r["error_code"])),
        "duration_seconds": round(time.monotonic() - started, 3),
        "recovery_seconds": None,
        "notes": (
            "固定协议基线：Mock按请求意图提出建议，违规建议由规则拦截；"
            "并非真实模型能力评分。恢复耗时见 worker-restart 演示。"
        ),
        "results": results,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{mode}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"# {mode} 固定协议评测",
        "",
        report["notes"],
        "",
        f"模型：{report['model']}；程序版本：{report['version']}；日期：{report['date']}。",
        f"总耗时：{report['duration_seconds']} 秒；"
        f"违规建议拦截：{report['blocked_refund_suggestions']}。",
        f"错误分类：{json.dumps(report['error_categories'], ensure_ascii=False)}。",
        "",
        f"样本 {len(results)}；结果匹配 {report['correct']}；"
        f"重复退款 {report['duplicate_refunds']}；"
        f"模型请求 {report['model_calls']}；token {report['tokens']}。",
        "",
        "| 样本 | 预期 | 实际 | 调用数 | 秒 |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {r['case_id']} | {r['expected_outcome']} | {r['outcome']} | "
        f"{r['model_calls']} | {r['duration_seconds']} |"
        for r in results
    ]
    (output / f"{mode}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if report["correct"] == len(results) and report["duplicate_refunds"] == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["mock", "http"], default="mock")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/evaluations")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.mode == "http" and not all(
        os.environ.get(k, "").strip() for k in ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME")
    ):
        args.output.mkdir(parents=True, exist_ok=True)
        report = {
            "mode": "http",
            "status": "not_run",
            "sample_count": 0,
            "tokens": None,
            "business_accuracy": None,
            "reason": "真实模型未验证：模型地址、名称或凭据未配置。",
        }
        (args.output / "http-unverified.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        parser.error("真实模型未验证：需配置 MODEL_BASE_URL、MODEL_API_KEY、MODEL_NAME")
    if args.inside:
        raise SystemExit(asyncio.run(evaluate(args.mode, args.output)))
    args.output.mkdir(parents=True, exist_ok=True)
    command = [
        "docker",
        "compose",
        "-p",
        "aftercare-eval",
        "-f",
        "compose.yaml",
        "-f",
        "compose.test.yaml",
    ]
    extra = []
    if args.mode == "http":
        for key in ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME"):
            extra += ["-e", key]
    try:
        result = subprocess.call(
            command
            + [
                "run",
                "--rm",
                "--build",
                *extra,
                "-v",
                f"{args.output.resolve()}:/reports",
                "tests",
                "uv",
                "run",
                "--no-sync",
                "python",
                "scripts/evaluate.py",
                "--inside",
                "--mode",
                args.mode,
                "--output",
                "/reports",
            ],
            cwd=ROOT,
        )
    finally:
        subprocess.call(command + ["down"], cwd=ROOT)
    raise SystemExit(result)


if __name__ == "__main__":
    main()
