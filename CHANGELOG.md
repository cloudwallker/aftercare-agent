# Changelog / 版本说明

## v0.1.0 — 2026-09-20

首个离线 MVP 发布版 / First offline MVP release.

### 功能 / Features

- 持久工单、客户与审核员鉴权、命令幂等及分页查询。
  Durable tasks, role-based access, idempotent commands, and pagination.
- 三类只读工具调查、不可变审批方案及重新调查。
  Read-only investigation, immutable approval proposals, and reassessment.
- 独立 Worker、租约与检查点、强杀接管和有界重试。
  Separate workers, leases, checkpoints, crash recovery, and bounded retries.
- 模拟商家退款账本、原 key 对账与订单级防重。
  Simulated merchant ledger, original-key reconciliation, and duplicate protection.
- SSE 断线续传、三条演示、只读核查及 HTTP 模型适配器。
  Resumable SSE, three demonstrations, read-only inspection, and HTTP model adapter.

### 验证 / Validation

236 tests passed in the full Docker/PostgreSQL suite. The fixed Mock baseline matched 20/20 cases, blocked 8 invalid refund suggestions, and produced 0 duplicate refunds. Clean deployment and all three demonstration commands passed.

完整记录见 [测试结果](docs/test-results.md)。真实模型端到端未验证；Mock 匹配率不是模型质量评分。Live external-model calls remain unverified; Mock results are not model-quality measurements.

### 发布范围 / Distribution

Source release with Docker configuration, migrations, scripts, tests, documentation, and MIT license. No virtual environment, local credentials, databases, logs, or runtime caches are included. This release does not process real payments.
