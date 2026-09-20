# A01～A22 验收对照

2026-09-20：标准 Docker / PostgreSQL 全套 **236 passed in 63.62s**。下表为实际测试文件（路径相对仓库）；`tests/integration` 使用真实 PostgreSQL，`tests/e2e` 包含真实 TCP/进程测试。外部模型仅通过契约测试，未运行真实模型评测。

| 编号 | 验收主题 | 实际测试证据 |
| --- | --- | --- |
| A01 | 完整批准退款 | tests/e2e/test_demo.py；tests/integration/test_refund_executor.py |
| A02 | 审核拒绝无退款 | tests/integration/test_approvals.py：test_rejection_creates_no_operation |
| A03 | 规则、金额、归属 | tests/integration/test_investigation.py；tests/unit/test_policy.py；tests/integration/test_api_access.py |
| A04 | 20 个同键创建 | tests/integration/test_task_commands.py：test_twenty_creates_replay_original_response |
| A05 | 决定幂等及竞争 | tests/integration/test_approvals.py：test_twenty_duplicate_and_terminal_replay、test_distinct_keys_one_winner |
| A06 | 重查代次和竞争 | tests/integration/test_approvals.py：test_approve_reassess_race、test_reassess_resets_budget_and_old_approval |
| A07 | 工具完成后强杀、预算保持 | tests/e2e/test_investigation_crash.py；tests/integration/test_checkpoints.py |
| A08 | 多 Worker 领取 | tests/integration/test_leases.py：test_two_workers_claim_exactly_one_lease |
| A09 | 过期 epoch 写回 | tests/integration/test_leases.py；tests/integration/test_refund_executor.py：test_stale_worker_cannot_commit_after_external_success |
| A10 | 成功响应丢失 | tests/e2e/test_demo.py：lost-response；tests/e2e/test_merchant_http.py |
| A11 | HTTP 前强杀 | tests/e2e/test_refund_crash_windows.py：before_refund_http |
| A12 | 外部成功本地未提交强杀 | tests/e2e/test_refund_crash_windows.py：after_refund_http_before_commit |
| A13 | 跨工单/商家订单防重 | tests/integration/test_refund_executor.py：test_two_tasks_reserve_same_order；tests/integration/test_merchant_refunds.py |
| A14 | 同 key 异参 | tests/integration/test_merchant_refunds.py：test_same_key_changed_parameters_conflicts |
| A15 | 冻结前审批过期 | tests/integration/test_approvals.py；tests/integration/test_refund_executor.py：test_expiry_before_and_after_freeze |
| A16 | 冻结后继续对账 | tests/integration/test_refund_executor.py：test_expiry_before_and_after_freeze、test_scanner_expires_unfrozen_but_preserves_queued_operation |
| A17 | 订单版本变化 | tests/integration/test_refund_executor.py：test_version_change_is_definite_decline |
| A18 | 有界不确定结果 | tests/integration/test_refund_executor.py：test_bounded_uncertainty、test_final_post_lost_response_still_gets_reconciled |
| A19 | 非法模型输出/假证据/预算 | tests/unit/test_http_provider.py；tests/integration/test_investigation.py；tests/integration/test_checkpoints.py |
| A20 | SSE 交错、重连、终态 | tests/integration/test_events.py；tests/e2e/test_events_tcp.py（真实 15 秒心跳） |
| A21 | 多表事务回滚 | tests/integration/test_checkpoints.py、test_investigation.py、test_approvals.py、test_refund_executor.py 中事件/提交故障用例 |
| A22 | 重启、seed、角色隔离 | tests/e2e/test_merchant_http.py；tests/integration/test_seed.py；tests/integration/test_schema.py；干净 Compose 部署记录 |

演示命令、固定评测、干净部署的实际结果见 [测试记录](test-results.md)，复现步骤见 [演示指南](demo.md)。
