"""审批和重新调查命令；统一 task→approval 锁顺序。"""

from sqlalchemy import and_, exists, func, or_, select

from aftercare.auth import require_client
from aftercare.commands import DomainError, run_command
from aftercare.contracts import DecisionResult, ReassessResult, canonical_hash
from aftercare.events import append_event
from aftercare.models import Approval, RefundOperation, Task
from aftercare.runtime.leases import clear_lease
from aftercare.tasks import fields, visible_query, visible_task

APPROVAL_FIELDS = (
    "id task_id generation status payload payload_hash summary expires_at decided_by decided_at"
)


def conflict(code="APPROVAL_CONFLICT"):
    return DomainError(409, code, "当前审批或工单版本不允许该操作。")


async def decide_approval(database, actor, approval_id, data, key):
    if actor.role != "reviewer":
        raise DomainError(403, "FORBIDDEN", "审批决定仅允许审核员执行。")
    payload = {"approval_id": str(approval_id), **data.model_dump(mode="json")}

    async def action(session):
        task_id = await session.scalar(select(Approval.task_id).where(Approval.id == approval_id))
        if task_id is None:
            raise DomainError(404, "APPROVAL_NOT_FOUND", "未找到审批。")
        task = await session.scalar(select(Task).where(Task.id == task_id).with_for_update())
        approval = await session.scalar(
            select(Approval).where(Approval.id == approval_id).with_for_update()
        )
        now = await session.scalar(select(func.clock_timestamp()))
        if (
            task.status != "waiting_approval"
            or approval.status != "pending"
            or approval.generation != task.generation
            or data.generation != task.generation
            or data.payload_hash != approval.payload_hash
            or canonical_hash(approval.payload) != approval.payload_hash
        ):
            raise conflict()
        if approval.expires_at <= now:
            raise conflict("AUTHORIZATION_EXPIRED")
        approval.status = "approved" if data.decision == "approve" else "rejected"
        approval.decided_by, approval.decided_at = actor.subject, now
        approval.decision_key = key
        approval.decision_hash = canonical_hash({"reviewer_id": actor.subject, **payload})
        task.updated_at = now
        clear_lease(task)
        if data.decision == "approve":
            task.status, task.phase, task.next_run_at = "queued", "execute_refund", now
        else:
            task.status, task.error_code, task.error_detail = (
                "rejected",
                "APPROVAL_REJECTED",
                "审核员拒绝了退款方案。",
            )
        await append_event(
            session, task, f"approval.{approval.status}", {"approval_id": str(approval.id)}
        )
        return 200, {
            "approval_id": str(approval.id),
            "task_id": str(task.id),
            "generation": task.generation,
            "status": approval.status,
        }

    result = await run_command(
        database, actor, f"/v1/approvals/{approval_id}/decision", key, payload, action
    )
    return DecisionResult(**result.body, replayed=result.replayed)


async def reassess(database, actor, task_id, data, key):
    require_client(actor)

    async def action(session):
        task = await session.scalar(
            visible_query(actor).where(Task.id == task_id).with_for_update()
        )
        if task is None:
            raise DomainError(404, "TASK_NOT_FOUND", "未找到工单。")
        approval = await session.scalar(
            select(Approval)
            .where(Approval.task_id == task.id, Approval.generation == task.generation)
            .with_for_update()
        )
        if (
            task.status != "waiting_approval"
            or task.generation != data.expected_generation
            or task.generation >= 3
            or approval is None
            or approval.status != "pending"
        ):
            raise conflict("REASSESS_CONFLICT")
        now = await session.scalar(select(func.clock_timestamp()))
        approval.status = "invalidated"
        await append_event(session, task, "approval.invalidated", {"approval_id": str(approval.id)})
        task.generation += 1
        task.lease_epoch += 1
        task.status, task.phase = "queued", "investigate"
        task.model_call_count = task.tool_call_count = 0
        task.generation_started_at = None
        task.result = task.error_code = task.error_detail = None
        task.next_run_at = task.updated_at = now
        clear_lease(task)
        task.checkpoint = {
            "schema_version": 1,
            "generation": task.generation,
            "messages": [],
            "next_step_no": 1,
            "evidence_by_tool": {},
            "invalid_output_count": 0,
        }
        await append_event(session, task, "task.reassessed", {})
        return 202, {"task_id": str(task.id), "accepted_generation": task.generation}

    result = await run_command(
        database, actor, f"/v1/tasks/{task_id}/reassess", key, data.model_dump(mode="json"), action
    )
    return ReassessResult(**result.body, replayed=result.replayed)


async def list_approvals(database, actor, task_id):
    async with database.sessions() as session:
        await visible_task(session, actor, task_id)
        rows = (
            await session.scalars(
                select(Approval).where(Approval.task_id == task_id).order_by(Approval.generation)
            )
        ).all()
        return {"items": [fields(row, APPROVAL_FIELDS) for row in rows]}


async def expire_approvals(database, limit=100):
    """短批次扫描；不处理 running，也不触碰已冻结的退款操作。"""
    async with database.sessions.begin() as session:
        tasks = (
            await session.scalars(
                select(Task)
                .where(
                    or_(
                        Task.status == "waiting_approval",
                        and_(Task.status == "queued", Task.phase == "execute_refund"),
                    ),
                    ~exists(select(RefundOperation.id).where(RefundOperation.task_id == Task.id)),
                    exists(
                        select(Approval.id).where(
                            Approval.task_id == Task.id,
                            Approval.generation == Task.generation,
                            Approval.status.in_(["pending", "approved"]),
                            Approval.expires_at <= func.clock_timestamp(),
                        )
                    ),
                )
                .order_by(Task.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        expired = 0
        for task in tasks:
            # 领取/冻结可能在扫描语句的快照之后提交，持锁后以新语句重查。
            if await session.scalar(
                select(RefundOperation.id).where(RefundOperation.task_id == task.id)
            ):
                continue
            approval = await session.scalar(
                select(Approval)
                .where(Approval.task_id == task.id, Approval.generation == task.generation)
                .with_for_update()
            )
            now = await session.scalar(select(func.clock_timestamp()))
            if approval.status not in {"pending", "approved"} or approval.expires_at > now:
                continue
            approval.status = "expired"
            task.status, task.error_code, task.error_detail = (
                "rejected",
                "AUTHORIZATION_EXPIRED",
                "退款授权已过期。",
            )
            task.updated_at = now
            clear_lease(task)
            await append_event(session, task, "approval.expired", {"approval_id": str(approval.id)})
            expired += 1
        return expired
