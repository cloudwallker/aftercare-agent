"""人工核查只读取冻结请求和原键账本，不改变任何本地状态。"""

from sqlalchemy import select

from aftercare.models import RefundOperation


async def inspect_refund(database, merchant, task_id):
    async with database.sessions() as session:
        operation = await session.scalar(
            select(RefundOperation).where(RefundOperation.task_id == task_id)
        )
        if operation is None:
            raise ValueError("任务没有冻结退款操作")
        key, request, status = operation.operation_key, operation.request, operation.status
    result = await merchant.lookup_refund(key, 5)
    return {
        "task_id": str(task_id),
        "operation_key": key,
        "local_status": status,
        "request": request,
        "merchant_result": result,
    }
