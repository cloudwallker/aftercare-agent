"""冻结授权→查询原键→同键重发；每次本地写入都重新 fencing。"""

import asyncio
import copy
from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aftercare.contracts import ConfirmedRefund, DeclinedRefund, RefundRequest, canonical_hash
from aftercare.events import append_event
from aftercare.models import Approval, RefundOperation
from aftercare.refunds.merchant_client import MerchantError
from aftercare.runtime.leases import clear_lease, locked_task


async def end_task(session, task, now, status, code):
    task.status, task.error_code = status, code
    task.error_detail = (
        "退款结果需人工核对。" if status == "manual_review" else "退款授权或执行条件不满足。"
    )
    task.result, task.updated_at = None, now
    clear_lease(task)
    await append_event(
        session,
        task,
        "task.manual_review" if status == "manual_review" else "task.failed",
        {"error_code": code},
    )


async def prepare_operation(database, lease):
    async with database.sessions.begin() as session:
        task, now = await locked_task(session, lease)
        if task.phase != "execute_refund":
            raise ValueError("不是退款执行阶段")
        operation = await session.scalar(
            select(RefundOperation).where(RefundOperation.task_id == task.id)
        )
        if operation:
            return operation.id
        approval = await session.scalar(
            select(Approval)
            .where(Approval.task_id == task.id, Approval.generation == task.generation)
            .with_for_update()
        )
        if approval is None or approval.status != "approved":
            await end_task(session, task, now, "rejected", "AUTHORIZATION_INVALID")
            return None
        if approval.expires_at <= now:
            approval.status = "expired"
            await append_event(session, task, "approval.expired", {"approval_id": str(approval.id)})
            await end_task(session, task, now, "rejected", "AUTHORIZATION_EXPIRED")
            return None
        data = approval.payload
        try:
            if (
                canonical_hash(data) != approval.payload_hash
                or data["task_id"] != str(task.id)
                or data["generation"] != task.generation
                or data["order_id"] != task.order_id
                or data["customer_id"] != task.customer_id
                or data["policy_version"] != "refund_v1"
                or data["currency"] != "CNY"
                or not 0 < data["amount_cents"] <= 100000
            ):
                raise ValueError
            request = RefundRequest(
                order_id=task.order_id,
                customer_id=task.customer_id,
                amount_cents=data["amount_cents"],
                currency=data["currency"],
                expected_order_version=data["order_version"],
                policy_version=data["policy_version"],
                authorization_id=approval.id,
            ).model_dump(mode="json")
        except (KeyError, TypeError, ValueError):
            await end_task(session, task, now, "rejected", "AUTHORIZATION_INVALID")
            return None
        operation_id = await session.scalar(
            insert(RefundOperation)
            .values(
                id=uuid4(),
                task_id=task.id,
                approval_id=approval.id,
                order_id=task.order_id,
                generation=task.generation,
                operation_key=f"refund:{task.id}:{task.generation}",
                request=request,
                request_hash=canonical_hash(request),
                status="prepared",
                reconcile_deadline=now + timedelta(seconds=120),
            )
            .on_conflict_do_nothing()
            .returning(RefundOperation.id)
        )
        if operation_id is None:
            await end_task(session, task, now, "rejected", "ORDER_REFUND_RESERVED")
            return None
        await append_event(session, task, "refund.prepared", {"operation_id": str(operation_id)})
        return operation_id


async def locked_operation(session, lease):
    task, now = await locked_task(session, lease)
    operation = await session.scalar(
        select(RefundOperation).where(RefundOperation.task_id == task.id).with_for_update()
    )
    if (
        operation is None
        or operation.generation != task.generation
        or task.phase != "execute_refund"
    ):
        raise ValueError("退款操作与任务不一致")
    return task, operation, now


async def reserve_http(database, lease, method):
    async with database.sessions.begin() as session:
        task, operation, now = await locked_operation(session, lease)
        remaining = (operation.reconcile_deadline - now).total_seconds()
        if remaining <= 0 or (method == "GET" and operation.reconcile_count >= 8):
            await end_task(session, task, now, "manual_review", "REFUND_RECONCILE_EXHAUSTED")
            return None
        if method == "POST" and (operation.dispatch_count >= 3 or operation.reconcile_count >= 8):
            return None
        if method == "GET":
            operation.reconcile_count += 1
            event = "refund.reconciling"
        else:
            operation.status = "unknown"
            operation.dispatch_count += 1
            event = "refund.dispatching"
        operation.updated_at = task.updated_at = now
        await append_event(
            session,
            task,
            event,
            {
                "operation_id": str(operation.id),
                "operation_key": operation.operation_key,
                "dispatch_count": operation.dispatch_count,
                "reconcile_count": operation.reconcile_count,
            },
        )
        return operation.operation_key, copy.deepcopy(operation.request), min(5, remaining)


def validate_result(operation, data):
    try:
        result = (
            ConfirmedRefund if data.get("status") == "confirmed" else DeclinedRefund
        ).model_validate(data)
        if (
            result.operation_key != operation.operation_key
            or result.request_hash != operation.request_hash
        ):
            raise ValueError
        if isinstance(result, ConfirmedRefund) and (
            result.order_id != operation.order_id
            or result.amount_cents != operation.request["amount_cents"]
            or result.currency != operation.request["currency"]
        ):
            raise ValueError
        return result.model_dump(mode="json")
    except (ValidationError, ValueError, AttributeError):
        raise MerchantError("REFUND_RESPONSE_MISMATCH") from None


async def commit_result(database, lease, data):
    async with database.sessions.begin() as session:
        task, operation, now = await locked_operation(session, lease)
        result = validate_result(operation, data)
        operation.status, operation.merchant_result = result["status"], result
        operation.last_error_code = None
        operation.updated_at = task.updated_at = now
        clear_lease(task)
        if result["status"] == "confirmed":
            operation.merchant_refund_id = UUID(result["refund_id"])
            task.status, task.error_code, task.error_detail = "succeeded", None, None
            task.result = {
                "outcome": "refunded",
                "refund_id": result["refund_id"],
                "amount_cents": result["amount_cents"],
                "currency": result["currency"],
            }
        else:
            operation.last_error_code = result["error_code"]
            task.status, task.error_code, task.error_detail = (
                "rejected",
                result["error_code"],
                "商家明确拒绝本次退款。",
            )
            task.result = None
        await append_event(
            session, task, f"refund.{result['status']}", {"operation_id": str(operation.id)}
        )
        await append_event(
            session,
            task,
            "task.completed" if task.status == "succeeded" else "task.failed",
            {"outcome": "refunded"}
            if task.status == "succeeded"
            else {"error_code": task.error_code},
        )


async def defer_reconcile(database, lease, code):
    async with database.sessions.begin() as session:
        task, operation, now = await locked_operation(session, lease)
        operation.last_error_code, operation.updated_at = code, now
        if operation.reconcile_count >= 8 or operation.reconcile_deadline <= now:
            await end_task(session, task, now, "manual_review", "REFUND_RECONCILE_EXHAUSTED")
            return
        delay = (2, 4, 8, 16, 30, 30)[min(max(operation.reconcile_count - 1, 0), 5)]
        task.status, task.updated_at = "queued", now
        task.next_run_at = min(now + timedelta(seconds=delay), operation.reconcile_deadline)
        clear_lease(task)
        await append_event(session, task, "task.retry_scheduled", {"error_code": code})


async def execute_refund(database, lease, merchant, *, hook=None):
    """hook 仅由测试组合根注入；业务输入或环境变量不能启用故障点。"""
    if await prepare_operation(database, lease) is None:
        return
    query = await reserve_http(database, lease, "GET")
    if query is None:
        return
    key, request, timeout = query
    try:
        async with asyncio.timeout(timeout):
            result = await merchant.lookup_refund(key, timeout)
        if result is not None:
            await commit_result(database, lease, result)
            return
        dispatch = await reserve_http(database, lease, "POST")
        if dispatch is None:
            # reserve_http 可能已因期限耗尽进入终态。
            from aftercare.runtime.leases import LeaseLost

            try:
                await defer_reconcile(database, lease, "REFUND_DISPATCH_EXHAUSTED")
            except LeaseLost:
                pass
            return
        key, request, timeout = dispatch
        if hook:
            await hook("before_refund_http", lease)
        async with asyncio.timeout(timeout):
            result = await merchant.post_refund(key, request, timeout)
        if hook:
            await hook("after_refund_http_before_commit", lease)
        await commit_result(database, lease, result)
    except (MerchantError, TimeoutError) as exc:
        await defer_reconcile(database, lease, getattr(exc, "code", "REFUND_HTTP_TIMEOUT"))
