# Aftercare Agent

简体中文 | [English](README.en.md)

**v0.1.0 · MIT** — 带人工审批、故障恢复和幂等对账的模拟售后退款 Agent。

已实现持久任务、只读调查 Agent、人工审批与模拟退款对账。独立 Worker 使用 MockProvider 生成待审批方案，审核员批准后冻结原请求并按幂等键执行、查询退款；结果无法确定时进入人工核查。已提供 SSE 断线续传、三条故障演示及 HTTP 工具调用模型适配器。任务 1～8 的离线 MVP 已完成；真实模型端到端尚未验证。

## 阅读顺序

1. [完整项目设计](docs/superpowers/specs/2026-09-18-aftercare-agent-design.md)：业务范围、参考项目、架构、8 张表、状态机、API、可靠性协议和 22 组验收用例。
2. [分阶段实施计划](docs/superpowers/plans/2026-09-18-aftercare-agent-implementation.md)：8 个任务、文件职责、测试示例、依赖关系和交接指令。

## 项目定位

一个参考 nanobot 的小型售后工单 Agent 后端。Agent 查询模拟订单和物流，提出退款建议；人工审批后由确定性执行器完成模拟退款。

重点展示三项后端能力：

- 人工审批绑定不可变方案及版本。
- 独立 Worker 的持久任务、检查点和故障恢复。
- 外部操作结果未知时，原幂等键对账与重复退款防护。

采用 Python 3.12、FastAPI、PostgreSQL。默认无模型 Key 也能演示；真实模型接入是可配置能力。第一版不做前端、真实支付、Redis、多 Agent 或 RAG。

## 本地单元测试

Python 3.12 与依赖固定于 `uv.lock`，测试框架单独放在 dev 依赖组。

```powershell
uv sync --locked
uv run pytest tests/unit -q
uv run ruff check .
```

若 Windows 中 uv 用户缓存目录不可写，先设置项目内缓存：

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.cache/uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path (Get-Location) '.cache/python'
```

配置通过 `load_settings()` 按 `APP_ROLE=api/worker/merchant/migrate/seed` 读取，各角色仅保存自身所需连接和密钥。导入模块不会连接数据库或启动进程。真实模型配置模式名称为 `MODEL_MODE=http`，缺少模型字段时校验失败；Worker 使用 HTTP 兼容适配器，不会自动回退 Mock。

## PostgreSQL 集成测试

启动 Docker Desktop。测试使用独立 Compose project、`aftercare_test` 数据库和专属卷；不映射数据库宿主端口，不需要复制开发 `.env`。

```powershell
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml up -d postgres
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml run --rm --build migrate
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml run --rm --build tests
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml down
```

`down` 保留测试卷。集成测试通过独立对象与事务回滚清理，不删除开发数据。宿主直接跑集成测试必须显式提供 `TEST_APP_DATABASE_URL`、`TEST_MERCHANT_DATABASE_URL`，库名必须以 `_test` 结尾，角色分别为 `aftercare_app` 与 `aftercare_merchant`。商家并发和进程测试还需要 `TEST_MIGRATION_DATABASE_URL`，用于只清理本测试创建的订单和账本，且必须连接同一测试库。配置缺失会明确报错，不会静默跳过。

## 启动模拟商家

```powershell
Copy-Item .env.example .env
docker compose up -d postgres
docker compose run --rm --build migrate
docker compose run --rm --build seed
docker compose up -d --build merchant
docker compose up -d --build api
docker compose up -d --build worker
```

商家监听容器内 `8001`，不映射宿主端口。内部服务接口：

| 接口 | 行为 |
| --- | --- |
| `GET /health/live`、`GET /health/ready` | 存活、迁移及数据库就绪 |
| `GET /orders/{id}?customer_id=...` | 按客户归属读取订单 |
| `GET /orders/{id}/shipment?customer_id=...` | 读取物流及共同订单版本 |
| `POST /refunds` | Bearer 商家 Token 与 Idempotency-Key；同键同参重放，异参 409 |
| `GET /refunds/by-key/{key}` | 查询原键 confirmed/declined 终态，不产生副作用 |

订单变化与账本终态在同一事务提交。HTTP 返回 503 不能说明退款未发生，应用执行器会查询原键对账。故障模式仅通过测试工厂注入，普通服务和 HTTP 请求没有启用入口。

seed 默认初始化 `ORD-1001`～`ORD-1006`，已退款样例同时写入商家账本。重复运行不会覆盖已有订单。可用 `docker compose run --rm seed uv run --no-sync python -m mock_merchant.seed --prefix demo-001-` 创建另一组模拟订单；重复使用相同前缀仍保持幂等。

API 默认监听 `http://127.0.0.1:8000`，Swagger 在 `/docs`。端口冲突时设置 `API_PORT`，不要停止其他服务。支持 `POST /v1/tasks`（必须带 Idempotency-Key）、`GET /v1/tasks`、任务详情及 `/steps` 分页。创建成功为 201；同键同参重放原响应，异参 409。请求 Bearer Token 来自 CLIENT_TOKEN 或 REVIEWER_TOKEN，只有客户能创建。

启动独立 Worker 后，新任务由 queued 进入调查。默认 MockProvider 对包含“退款”的消息提出退款建议；其他消息在完成三类查询后返回 no_action。符合规则的退款建议进入 waiting_approval，详情接口返回不可变方案、摘要和 24 小时有效期；不符合规则则 rejected/POLICY_DENIED。此演示判断不代表真实模型能力。

API 不会自动启动 Worker。审核员通过 `POST /v1/approvals/{id}/decision` 提交 `generation`、`payload_hash` 和 `decision=approve/reject`；字段从任务详情中的当前审批读取。批准只排队，退款由独立 Worker 执行。客户可在 waiting_approval 时通过 `POST /v1/tasks/{id}/reassess` 携带 `expected_generation` 重新调查，最多三代；历史审批由 `/v1/tasks/{id}/approvals` 查询。两个写接口均需 Idempotency-Key，成功命令可原样重放。

退款最多三次 POST、八次 GET，冻结后总期限 120 秒；始终先查询原键，响应不确定时保留原请求和订单占用。预算耗尽进入 manual_review，不能通过另建工单绕过占用。租约、审批和对账接口说明见 [runtime.md](docs/runtime.md)。

迁移版本 `0001_initial` 创建 `app` 与 `merchant` 两个 schema。业务连接只有所属 schema 的读写权限和 `public.alembic_version` 的只读权限；不能跨 schema 访问。金额使用整数分，时间使用 UTC `timestamptz`，应用事务使用 READ COMMITTED。

本地退款操作通过部分唯一索引保留订单占用，商家账本通过独立部分唯一索引限制每订单仅一条成功退款。不可变审批与冻结请求由数据库触发器保护。HTTP 查询、请求和模型调用可能重复；退款效果依赖商家幂等收敛，不宣称整个系统 exactly-once。

`.env.example` 和初始化 SQL 中的值仅是本地演示凭据。实际完成范围见 [实施进度](docs/implementation-progress.md)，验证结果见 [测试记录](docs/test-results.md)，参考来源见 [来源说明](docs/references.md)。真实模型端到端尚未验证。

## 演示与评测

启动 Docker Desktop 后执行：

```powershell
uv run python scripts/demo.py --scenario happy-path
uv run python scripts/demo.py --scenario lost-response
uv run python scripts/demo.py --scenario worker-restart
uv run python scripts/evaluate.py --mode mock
```

演示自动创建隔离测试服务，结束后关闭服务并保留数据卷。固定 20 条样本验证协议行为，不代表真实模型业务准确率。完整操作、只读核查及 HTTP 配置见 [演示指南](docs/demo.md)，逐项验收见 [A01～A22 对照表](docs/acceptance.md)。

`GET /v1/tasks/{id}/events` 返回 SSE；携带 Bearer Token 和可选 `Last-Event-ID` 继续读取。每 15 秒发送注释心跳，终态事件追平后关闭。浏览器可使用支持 Bearer 请求头的流式 fetch 客户端。

## 源码结构与发布

源码按职责位于 `src/aftercare/` 与 `src/mock_merchant/`；数据库迁移在 `migrations/`，演示和评测在 `scripts/`，单元/集成/进程测试在 `tests/`，设计与验收证据在 `docs/`。

项目采用 [MIT 许可证](LICENSE)。参见 [版本说明](CHANGELOG.md)、[源码发布规则](docs/release.md) 和 [英文简介](README.en.md)。依赖保持各自许可证。
