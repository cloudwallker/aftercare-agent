# 运行时与执行边界

本阶段提供持久任务 API、Worker 调度、只读调查、审批命令及退款执行 handler。

## 创建与查询

`create_task(database, actor, data, key)` 不访问商家。一个 READ COMMITTED 事务内占用命令幂等键、创建任务、保存 `task.created` 和原始响应。冲突后通过新语句读取已提交响应；同键同参重放原始 queued，不受当前任务状态影响。

API 按 Token 派生客户或审核员身份。客户只见自己的工单，审核员能查询所有工单但不能创建。详情隐藏 lease_owner、checkpoint；列表采用 `(created_at,id)` 游标，步骤采用 `(generation,step_no)` 游标。

## 领取和写回

`claim_next(database, worker_id)` 使用 `FOR UPDATE SKIP LOCKED`，只领取到期 queued 或租约过期 running。获得行锁后再读 PostgreSQL `clock_timestamp()`，增加 epoch 并设置 lease。调查代的开始时间仅在首次领取时设置。

`locked_task(session, lease)` 是所有 Worker 写回的共同入口：先锁 task，再读数据库时间，检查 owner、epoch、running、expiry。即使没有别人接管，过期 lease 也不能续租复活。此检查保护本地数据库，不能取消已发送的 HTTP。

`append_event(session, task, type, data)` 不自行提交，调用方必须已持有同一事务的 task 行锁，或刚插入该任务。序号在 task 行内递增，和状态变更一并回滚；禁止绕过此锁自行分配 seq。

## 步骤预扣与完成

1. `reserve_step(...)` 在有效租约下创建或恢复当前 pending 步骤，并预扣调用预算。
2. 关闭事务后，handler 调用模型/只读工具。有效超时采用返回的 `timeout_seconds`；它不超过模型 30 秒、工具 5 秒及调查剩余 180 秒预算。
3. `complete_step(...)` 在新事务中重新验证租约和尝试编号，原子保存 step、checkpoint、`step.completed`。

每次实际模型请求预扣 model_call_count；新工具逻辑步骤才预扣 tool_call_count。恢复同一 pending 步骤不重置计数。远程步骤最多三次尝试；本地规则查询不消耗 HTTP 尝试数，但占一个工具逻辑额度。

显式指定已经 succeeded 的 step_no 时返回 `reused=True` 和原输出，不增加计数；调用方必须直接使用结果。已完成步骤输出不可覆盖。不同 kind/name/input 不能替换 pending 选择，较旧尝试不能提交到新尝试。

checkpoint v1 保存本代 `messages`、`next_step_no`、`evidence_by_tool` 和 `invalid_output_count`。messages 是有界的已提交结果记录（step_id/kind/name/output），不是模型隐藏推理。任务 4 的 runner 应从已保存的模型动作恢复后续工具，不能重新询问模型替换已选动作。工具输出最多 16 KiB、模型输出最多 8 KiB、整个 checkpoint 最多 128 KiB。

## Worker 组合

`Worker(database, worker_id, handlers={"investigate": handler}, ...)` 的 `once()` 与 `run(stop_event)` 使用相同分派流程。handler 接收 Lease，使用上述函数写回，并自行完成状态转换或 `schedule_retry`。handler 返回时如果仍持有运行租约，任务会明确失败为 `HANDLER_INCOMPLETE`。

本地信号量在领取前获取；handler 等待外部调用时不持有数据库事务。独立心跳任务续租，续租失败会取消 handler 并保留数据库状态供后续接管。已冻结 operation 后发生异常进入 manual_review，而不伪装为确定退款失败。

`serve(worker)` 接收 SIGTERM/SIGINT 后停止继续领取，最多等待 10 秒，再取消未完成处理及心跳。退款强杀窗口的验证记录见 test-results.md；调查阶段已完成工具后的真实强杀接管也已通过。

`APP_ROLE=worker` 下的 `python -m aftercare.worker` 注入调查及退款两个 handler。调查只接收 MerchantClient，执行器接收 RefundMerchantClient；模型工具表不包含退款方法。Compose worker 只接收应用数据库连接与商家 Token。HTTP 模型模式注入独立 HttpProvider 和模型连接，不与商家连接共用 Token，也不静默回退 Mock。

## 调查与方案

`run_investigation(database, lease, provider, merchant)` 从当前代 checkpoint 恢复：已保存的模型选择直接转工具调用，pending 使用原输入和尝试数，成功工具不再调用；已保存的最终建议直接尝试提交业务结果。

每次模型响应经动作 Schema 和证据校验。非法输出仅保存固定错误码，和该模型步骤完成、纠正计数在同一事务提交；第二次非法输出结束为 INVALID_MODEL_OUTPUT。三类证据必须是当前工单、当前代、各工具最新成功步骤，订单与物流版本必须一致。版本不一致反馈给模型重新查询；持续不一致耗尽预算则 SNAPSHOT_UNSTABLE。

Provider 与远程工具调用使用硬超时。可重试错误释放租约并持久排队，退避 1 秒、3 秒；恢复仍使用同一逻辑步骤，最多三次尝试。外部错误仅保留固定代码，不保存 Token、响应原文或连接串。工具不接受模型提供的订单、客户或 URL。

`evaluate_refund` 校验 refund_v1；金额、币种、客户与版本均从已验证快照生成。`finish` 在有效租约与本代预算内锁定任务，将不可变 approval、waiting_approval 和 approval.requested 事件同事务提交；方案哈希采用规范 JSON，证据 ID 排序，审批有效期为数据库当前时间后 24 小时。no_action 同样要求三类证据；规则不满足时说明由程序生成。所有结束路径释放租约，调查不创建 refund_operation，也不发送退款 POST。

## 审批与重新调查

`decide_approval` 只接受 reviewer，在命令幂等事务中按 task→approval 锁顺序校验代次、哈希、待审批状态与数据库当前时间。批准转 queued/execute_refund，拒绝转 rejected；API 不调用商家。已提交命令在终态之后仍重放原响应，不重新修改决定。

`reassess` 只接受当前客户且任务须 waiting_approval，最多三代。在同一事务失效旧审批、generation/epoch 加一、清空本代 checkpoint 和调用计数、generation_started_at 设 NULL，并保留旧步骤与事件。审批和重新调查竞争只有一个提交，另一方 409。

Worker 独立扫描每五秒调用 `expire_approvals`，每批最多 100 条并 SKIP LOCKED。只处理 waiting_approval 或 queued/execute_refund 且未冻结 operation 的过期授权；持锁后用新语句再次检查 operation。running 由租约持有者处理，冻结后的对账不受审批到期影响。

## 退款执行与不确定结果

`prepare_operation` 在有效租约下校验当前 approved 快照，建立唯一 operation；key 固定为 refund:task_id:generation，请求和哈希、授权及 120 秒期限由数据库触发器保护。另一任务已占同订单则 rejected/ORDER_REFUND_RESERVED；prepared、unknown、confirmed 均保留占用，declined 释放占用。

`execute_refund` 每次恢复先预扣 GET 计数并查询原键。返回 confirmed/declined 必须通过 Schema、key、request_hash 校验；成功凭证还要匹配订单、金额、币种和 refund_id。只有 GET missing 才允许同键同参 POST，发送前先提交 unknown、dispatch_count 和事件。每次 HTTP 硬超时取五秒和剩余对账时间的较小值。

最多三次 POST、八次 GET；POST 用完继续 GET，最后一次 GET missing 后不再 POST。失败或不确定响应按 2/4/8/16/30/30 秒持久退避，释放租约；next_run_at 不超过总期限。GET/期限耗尽进入 manual_review，保留原操作和订单占用。冻结后程序异常也进入 manual_review，不能伪报确定失败。

本地 operation 结果、task 终态和事件同一事务提交，重新验证 owner/epoch/租约有效期。旧 Worker 已发出的外部请求不能撤回，但必须复用同一请求，由商家幂等约束合并副作用；旧 Worker 的本地写回被拒。测试 Hook 只通过 execute_refund 的内部参数注入，普通环境与业务输入无法启用。

## SSE 与日志

先读取任务状态与 high-water H，再按 `cursor < seq <= H` 有序读取，每批最多 100 条；终态追平 H 后关闭。每秒轮询、每 15 秒注释心跳，等待及发送期间不持有数据库事务。Last-Event-ID 必须是非负 int64 ASCII 整数；超当前序号返回 409。客户仅能读取自身任务，不存在和不可见均返回 404。

Worker 的 `event_staged` JSON 日志含关联 ID、代次、epoch、步骤、操作 key、事件序号、固定错误码和耗时，不记录凭据或响应原文。日志发生在 flush 后、commit 前，可能对应后来回滚的事务；已提交的数据库事件才是权威记录。

## HTTP 模型适配器

向配置地址追加 `/chat/completions` 发起请求，关闭并行工具调用；一次只接受一个工具动作或严格 FinalAction。持久化历史重建 assistant tool_calls 和对应 tool result，调用 ID 由模型步骤 UUID 稳定派生，恢复后保持配对。模型隐藏推理不会保存。

适配器单次请求不重试。429、5xx 和网络超时交由 runtime 持久预算与退避处理；错误输出进入最多一次纠正。地址、名称和密钥仅从 Worker 配置读取。工具结果携带实际证据步骤 ID，方案仍由后端验证。契约测试使用 MockTransport；真实模型效果和外部服务兼容性尚未验证。

`inspect-refund` 只读取本地任务/操作，并向商家 GET 原 key；不修改任务、计数、事件或账本，不自动解决 manual_review。
