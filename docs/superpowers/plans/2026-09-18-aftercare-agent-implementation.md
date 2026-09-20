# Aftercare Agent 实施计划

> 面向执行 Agent：用户审阅设计并授权实现后，按任务逐项执行。若当前环境有 superpowers 技能，使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans`；没有这些技能也必须遵循本文任务顺序、验收规则和用户指令。勾选项用于跟踪，不代表已完成。

**目标：** 实现一个售后退款 Agent 小型后端，能通过人工审批、进程恢复和不确定 HTTP 结果的故障测试。

**架构：** 一个仓库内的 FastAPI API、独立 Worker、模拟商家 HTTP 服务；PostgreSQL 保存任务、步骤、审批和事件，商家维护独立退款账本。调查阶段运行一个模型/工具循环，退款阶段运行确定性的幂等对账协议。

**技术栈：** Python 3.12、FastAPI、Pydantic 2、SQLAlchemy 2、psycopg 3、PostgreSQL 16、Alembic、httpx、uv、pytest、Ruff、Docker Compose。

**设计依据：** [完整项目设计](../specs/2026-09-18-aftercare-agent-design.md)。设计是行为事实来源；本文给出执行顺序和测试落点，必须同时阅读两份文件。

**当前状态：** 截至 2026-09-20，任务 1～8 离线 MVP 已完成，Docker 内 236 项测试通过；三条演示、干净部署和固定 Mock 20 条评测通过。真实模型端到端未验证（未配置凭据）。见 [测试结果](../../test-results.md)、[验收对照](../../acceptance.md) 与 [实施进度](../../implementation-progress.md)。

## 1. 全局约束

- Python 3.12；FastAPI；Pydantic 2；SQLAlchemy 2；psycopg 3 异步驱动；Alembic。
- PostgreSQL 16；应用事务隔离级别为 `READ COMMITTED`。
- 全部时间使用 PostgreSQL UTC `timestamptz`；金额使用整数分，禁止 float。
- 一个场景：已支付、物流延误订单的一次性全额模拟退款。
- `phase` 为 `investigate` 或 `execute_refund`。
- 只允许退款建议进入人工审批；模型不得直接调用退款执行器。
- 只依赖 API+Worker+模拟商家+PostgreSQL；不增加 Redis、多 Agent、RAG、前端或真实支付。
- 已提交的步骤可复用，未完成的模型和工具调用允许有界重复；不能宣称整个系统 exactly-once。
- 测试与文档使用中文说明，Python 标识符和错误码使用英文。
- 本地现有文件和用户修改必须保留；不强制重置仓库，不自动推送或部署公网。

每次实际执行先读取工作区 AGENTS.md 与用户最新指令。若设计已由用户批准，不再重复请求同一范围的许可。用户明确只要求计划或审阅时，停止在文档阶段。

## 2. 文件与边界

完整目录见设计第 12 节。重点所有权如下：

| 文件 | 责任 |
| --- | --- |
| `config.py`、`db.py` | 配置验证、角色连接、session factory |
| `models.py`、`contracts.py` | app 表映射及接口数据结构 |
| `commands.py` | API 命令幂等事务，不做模型或 HTTP 调用 |
| `tasks.py`、`approvals.py` | 创建、查询、审批、重新调查、过期 |
| `runtime/leases.py`、`runtime/steps.py` | 领取/续租校验、步骤与 checkpoint 提交 |
| `agent/provider.py`、`mock_provider.py`、`http_provider.py` | 模型统一动作接口和两个实现 |
| `agent/tools.py`、`agent/runner.py` | 白名单工具、调查循环 |
| `refunds/policy.py`、`merchant_client.py`、`executor.py` | 资格校验、HTTP 契约、冻结与对账 |
| `events.py`、`api.py`、`worker.py` | 事件持久化/SSE、应用入口、工作进程入口 |
| `mock_merchant/models.py`、`ledger.py`、`api.py`、`seed.py` | 外部模拟账本、HTTP 服务、演示数据 |

依赖方向：路由→业务函数→事务/外部客户端。policy 和 contracts 不依赖 FastAPI。模拟商家不导入 app 的 ORM 或业务事务；可共享纯契约和固定退款规则，不能共享应用审批对象的可变内存。

测试连接必须是独立测试库。运行前校验库名以 `_test` 结尾，任何清理仅限显式测试 schema。不能对开发库做清表。每测试创建独立订单/task，使用 fixture teardown 删除该测试对象或回收专属测试 schema；并发测试使用真实独立连接，不能包在一个外层未提交事务里。

标准环境采用设计 14.1 的 `compose.test.yaml`、aftercare-test project、aftercare_test 数据库和 tests 容器。下面各任务的 `uv run pytest ...` 可作为 tests 服务的覆盖命令执行；宿主执行需要自行配置明确的 TEST_APP_DATABASE_URL/TEST_MERCHANT_DATABASE_URL，不能偷偷连接开发库。

## 3. 共享测试设施

在 `tests/conftest.py` 定义：

- `client`：带 CLIENT_TOKEN 的 httpx.AsyncClient，指向应用 ASGI app。
- `reviewer`：带 REVIEWER_TOKEN 的同一应用客户端。
- `merchant_client`：带 MERCHANT_TOKEN 的商家 ASGI 客户端，连接真实测试数据库。
- `db`、`merchant_db`：分别使用对应测试角色的 SQLAlchemy AsyncSession，显式事务由测试控制。
- `seed_order(**overrides) -> OrderFixture`：通过 merchant 测试角色插入独立订单；对象有 id/customer_id/paid_amount_cents/order_version。
- `app_factory(provider, merchant_transport, hooks)`：创建应用和业务依赖；worker 使用同一依赖构造函数，但不会随 API 自动启动。
- `hooks`：测试专用 awaitable Barrier 集合，可在设计 13.3 的检查点暂停；生产默认为空操作，不能由工单请求启用。
- `worker_once(worker_id) -> bool`：运行一次领取并处理，返回是否领取到任务；内部使用与真实 Worker 完全相同的分派函数。

所有涉及计划中的 helper 都应按以上契约实现，不能用一个“返回预期结果”的假 helper 代替真实业务。以下代码是后续测试应写入的契约示例，不是当前已经存在的测试。

这些 fixture 随对应任务逐步加入；尚未实现的 api、provider 或 worker 不得在 conftest 顶层导入，避免任务 1 的迁移测试因未来模块缺失而不能收集。先建数据库 fixture，后续 fixture 通过函数内导入或组合根注入建立依赖。

## 4. 任务清单

### 任务 1：建立工程、数据约束与角色隔离

**文件：** 新建 `pyproject.toml`、`uv.lock`、`.gitignore`、`.env.example`、`Dockerfile`、`compose.yaml`、`compose.test.yaml`、`alembic.ini`、`docker/init-db.sql`、`migrations/env.py`、`migrations/versions/0001_initial.py`、两个 package 的 `__init__.py`、`src/aftercare/config.py`、`db.py`、`models.py`、`contracts.py`、`src/mock_merchant/models.py`、`tests/conftest.py`、`tests/integration/test_schema.py`。

**输入：** 设计第 3、7、14 节。\
**输出：** 8 张表、两个 schema、业务角色隔离；config 提供按进程角色加载配置，db 提供 session factory；contracts 定义 Actor、Lease、CreateTask、ApprovalDecision、ToolAction、FinalAction 和结果 DTO。

- [x] 读取依赖官方兼容说明，创建 Python 3.12 环境，锁定依赖；生产依赖不加入测试框架。
- [x] 写迁移 smoke test，确认无迁移时表检查失败，然后实现迁移与 CHECK/UNIQUE/FK/索引。
- [x] 建立 app 与 merchant 角色，编写越权访问测试。
- [x] `.env.example` 使用明确的本地演示值；按角色加载配置并拒绝业务进程管理员连接；API/Worker 进程本身在后续任务实现。
- [x] 运行对应虚拟环境 `python -m pytest tests/unit tests/integration tests/e2e -q --tb=short -p no:cacheprovider`，43 项通过；迁移版本和依赖版本见测试记录。

约束测试应包含真实重复插入：

```python
@pytest.mark.asyncio
async def test_app_role_cannot_read_merchant(db):
    with pytest.raises(DBAPIError):
        await db.execute(text("SELECT id FROM merchant.orders LIMIT 1"))
    await db.rollback()
```

该任务还需测试两种订单唯一约束：本地 prepared/unknown/confirmed 槽位和商家 confirmed 槽位。预期违反约束时抛 IntegrityError，不能仅检查索引名称存在。

**完成标准：** 空测试库可升级到 head；相同 head 再执行无变化；跨角色访问被拒；A22 的数据库部分通过。必要时拆分 ORM 文件，但不改变表协议。

### 任务 2：实现模拟商家的 HTTP 幂等账本

**文件：** 新建 `src/mock_merchant/api.py`、`ledger.py`、`seed.py`、`tests/integration/test_merchant_refunds.py`；完善 `src/aftercare/contracts.py`、`compose.yaml`。

**输入：** 任务 1 的表和连接，设计第 4、9.3、10.2 节。\
**输出：** `GET /orders/{id}`、shipment、`POST /refunds`、`GET /refunds/by-key/{key}`，同键同参重放；seed 幂等且不覆盖历史。

- [x] 先写同键同参、同键异参、新 key 同订单三种测试，验证它们在未实现 ledger 时失败。
- [x] 实现一个事务内 key 预留、订单锁、规则/版本校验、订单更新和终局账本保存。已有 key 在检查订单新状态之前返回原结果。
- [x] 对新 key 的确定拒绝保存 declined；不单独提交 processing；HTTP 只在提交后响应。
- [x] 实现持久化 `commit_then_503_once` 故障，仅测试配置可启用。
- [x] 已纳入 Docker 全套测试，包含 20 个独立连接同键/不同键并发请求及真实商家 TCP 进程重启。

```python
@pytest.mark.asyncio
async def test_same_key_replays_refund(merchant_client, seed_order):
    order = await seed_order()
    payload = {
        "order_id": order.id,
        "customer_id": order.customer_id,
        "amount_cents": order.paid_amount_cents,
        "currency": "CNY",
        "expected_order_version": order.order_version,
        "policy_version": "refund_v1",
        "authorization_id": str(uuid4()),
    }
    headers = {"Idempotency-Key": str(uuid4())}
    first = await merchant_client.post("/refunds", json=payload, headers=headers)
    second = await merchant_client.post("/refunds", json=payload, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["status"] == "confirmed"
```

额外断言商家账本 confirmed 数量为 1，订单版本只增加一次。不能只比较两个 HTTP 200 而不验证副作用。

**完成标准：** A10 的商家故障、A13 的直接并发、A14、A17 的商家部分通过。模拟商家不暴露宿主端口，也没有接入真实支付。

### 任务 3：持久任务、幂等创建、租约与检查点

**文件：** 新建 `src/aftercare/auth.py`、`commands.py`、`tasks.py`、`runtime/__init__.py`、`runtime/leases.py`、`runtime/steps.py`、`events.py`、`worker.py`、`api.py`、`tests/integration/test_task_commands.py`、`test_leases.py`、`test_checkpoints.py`。

**输入：** 任务 1 的 contracts/ORM，设计第 6～8、10.1 节。\
**输出：** `create_task`、`claim_next`、`renew_lease`，任务查询 API；`append_event` 在调用方 task 行锁事务内分配 seq；步骤预扣和提交函数共同验证 Lease。

- [x] 写 20 个并发创建请求的测试及同键异参测试；实现 command_requests 的业务响应原子保存与重放。
- [x] 写租约过期但尚未接管时续租失败、接管后旧 epoch 写入失败的测试。
- [x] 实现 SKIP LOCKED 领取、DB 时钟、心跳和本地并发槽位。HTTP 期间不持有事务。
- [x] 为模型/工具步骤实现 pending→succeeded 提交，checkpoint 与 event 在同事务；恢复 pending 时续用 attempt_count。仅新工具步骤增加逻辑调用数，模型每次实际请求都扣额度；持久化 invalid_output_count。
- [x] 写事务中插事件失败时 step/checkpoint 同时回滚的测试。
- [x] 核心集成测试及新增 Worker 测试已纳入标准 Docker 全套执行：110 passed，详见测试记录。

```python
@pytest.mark.asyncio
async def test_create_request_is_idempotent(client, seed_order):
    order = await seed_order()
    payload = {"order_id": order.id, "message": "物流延误，申请退款"}
    headers = {"Idempotency-Key": str(uuid4())}
    responses = await asyncio.gather(*[
        client.post("/v1/tasks", json=payload, headers=headers)
        for _ in range(20)
    ])
    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["task_id"] for r in responses}) == 1
```

租约时间测试优先通过独立数据库事务设置明确的过期时间，避免真实等待 20 秒；进程恢复端到端测试仍使用真实租约。

**完成标准：** A04、A08、A09 的本地 fencing、A21 通过。Worker 在本任务阶段仅提供可测试的分派入口；未接入 Agent 的阶段不得伪返回业务成功。

**验收说明：** 本任务已完成；A21 本阶段验证命令、步骤、checkpoint 与事件的事务原子性，审批与退款结果提交在后续任务验收。运行时调用契约见 [runtime.md](../../runtime.md)。

### 任务 4：只读 Agent、规则校验和待审批方案

**文件：** 新建 `src/aftercare/agent/__init__.py`、`provider.py`、`mock_provider.py`、`tools.py`、`runner.py`、`src/aftercare/refunds/__init__.py`、`policy.py`、`merchant_client.py`、`tests/unit/test_policy.py`、`test_agent_contracts.py`、`tests/integration/test_investigation.py`；完善 worker 和 contracts。

**输入：** 任务 2 的商家 API、任务 3 的 lease/step，设计第 4～6 节。\
**输出：** `ModelProvider.next_action`、三个工具和 `run_investigation(lease)`；规则校验函数 `evaluate_refund(order,shipment,customer_id) -> PolicyVerdict`；创建不可变 approval。

- [x] 固定 Pydantic 工具/最终建议 Schema，拒绝额外字段、非法工具、金额覆盖和假证据。
- [x] 实现 policy 纯函数及固定失败码；订单归属不符统一不可见。
- [x] MockProvider 通过相同工具链完成订单→物流→规则→最终建议。
- [x] runner 按 generation checkpoint 恢复；已持久化工具选择不重新问模型；每次调用前预扣预算。
- [x] 生成方案时验证三种证据、共同订单版本、资格，计算 payload_hash，在一个事务内保存 approval+waiting_approval+event。
- [x] 三个新增测试文件已纳入 Docker 全套回归：163 passed；额外独立服务 smoke 验证通过。

示例断言无需真实模型：

```python
def test_final_action_cannot_override_amount():
    with pytest.raises(ValidationError):
        FinalAction.model_validate({
            "kind": "final",
            "recommendation": "recommend_refund",
            "summary": "申请全额退款",
            "evidence_step_ids": [],
            "amount_cents": 1,
        })
```

集成测试需实际创建任务→worker_once→读取 approval；同时注入一代内不一致 order_version、模型无限循环及无效证据，断言无 operation 和退款 POST。

**完成标准：** A03、A19、A07 的步骤恢复部分通过；终止流程释放 lease，不把审批等待实现成睡眠中的协程。

### 任务 5：审批、版本失效与命令幂等

**文件：** 新建 `src/aftercare/approvals.py`、`tests/integration/test_approvals.py`；完善 tasks、commands、api、worker 的审批过期扫描。

**输入：** 任务 4 的 immutable approval、任务 3 的 command_requests，设计第 6.2、10.1 节。\
**输出：** `decide_approval`、`reassess`、历史审批查询及过期处理；批准排队到 execute_refund，尚不发送退款。

- [x] 写重复批准、批准/拒绝竞争、approve/reassess 竞争和旧版本审批测试。
- [x] 在 command_requests 事务内遵循 task→approval 锁顺序；验证 generation/hash/有效期及 reviewer 身份。
- [x] 实现 reassess 的 generation+1、lease_epoch+1、失效旧审批和最多三代约束；同事务清空上下文，重置本代调用计数、invalid_output_count 和 step_no，generation_started_at=NULL，保留历史 step 和 event。测试等待审批超过180秒后的重查不会继承旧代超时。
- [x] 实现已成功命令的状态码/业务 body 原样重放；不能因为当前任务进入终态就拒绝重放。
- [x] 实现过期扫描，只处理未冻结 operation 的授权；不能抢占有效运行 lease 写状态。
- [x] 审批集成测试已纳入 Docker 全套回归，详见测试记录。

```python
@pytest.mark.asyncio
async def test_old_approval_is_rejected_after_reassess(
    client, reviewer, seed_order, worker_once
):
    order = await seed_order()
    created = await client.post("/v1/tasks", json={
        "order_id": order.id, "message": "申请退款"
    }, headers={"Idempotency-Key": str(uuid4())})
    task_id = created.json()["task_id"]
    await worker_once("test-worker")
    task = (await client.get(f"/v1/tasks/{task_id}")).json()
    old = task["approval"]
    response = await client.post(f"/v1/tasks/{task_id}/reassess", json={
        "expected_generation": old["generation"]
    }, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 202
    rejected = await reviewer.post(f"/v1/approvals/{old['id']}/decision", json={
        "generation": old["generation"], "payload_hash": old["payload_hash"],
        "decision": "approve"
    }, headers={"Idempotency-Key": str(uuid4())})
    assert rejected.status_code == 409
```

**完成标准：** A02、A05、A06、A15 的审批部分通过；api 进程不会调用退款 HTTP。

### 任务 6：冻结退款操作、未知结果与恢复对账

**文件：** 新建 `src/aftercare/refunds/executor.py`、`tests/integration/test_refund_executor.py`、`tests/e2e/test_refund_crash_windows.py`；完善 merchant_client、worker、hooks。

**输入：** 任务 5 的批准、任务 2 的商家协议、任务 3 的 lease；设计第 9 节。\
**输出：** `execute_refund(lease)`，prepared/unknown/confirmed/declined，订单级活动槽位和 manual_review。

- [x] 验证商家成功但响应丢失、应用成功响应后提交前强杀及 HTTP 前强杀，测试直接查询商家账本计数。
- [x] 锁 task/approval，校验有效期与当前版本，插入唯一 operation，冻结原 key 和请求，记录 120 秒期限。
- [x] 恢复 prepared/unknown 先 GET；missing 后仅同键同参重发；发送前持久化 unknown 与 dispatch_count。
- [x] 验证响应 fingerprint 和成功字段；操作结果、task 终态、event 在有效 lease 的同一事务提交。
- [x] 实现预算/退避，达到预算保留订单占用进入 manual_review；程序异常不得把未知资金结果改 failed。
- [x] 实现两个工单争同订单本地约束，商家层独立防重测试持续通过。
- [x] 退款集成与真实进程强杀测试已纳入 Docker 全套：202 passed；标准 Compose 完整审批退款链路也通过。

恢复核心顺序必须能在代码中直接辨认：

```text
load/freeze immutable operation under current lease
GET original operation_key
  confirmed -> validate response -> atomic local completion
  declined  -> validate response -> atomic local rejection
  missing   -> persist unknown + attempt -> POST same key and same request
  uncertain -> persist retry schedule, or manual_review when exhausted
```

进程测试用明确 Hook 信号停在 `before_refund_http` 或 `after_refund_http_before_commit`，强杀指定测试 Worker，启动新 Worker，断言 operation_key 不变、只存在一条商家 confirmed、任务能在预算内结束。只能终止本测试启动并记录 PID 的进程。

**完成标准：** A01、A09 的外部竞态、A10～A18、A21 的结果提交部分全部通过。这是最高风险任务，任何未解决的并发/不确定结果缺陷均不能用日志警告代替修复。

### 任务 7：完整 API、SSE、部署和三条演示链路

**文件：** 完善 `src/aftercare/api.py`、`events.py`、`compose.yaml`、`Dockerfile`；新建 `tests/integration/test_api_access.py`、`test_events.py`、`tests/e2e/test_demo.py`、`scripts/demo.py`、`docs/demo.md`。

**输入：** 任务 1～6，设计第 10、11、14 节。\
**输出：** Swagger 可浏览所有接口，SSE 可补读，标准 Compose 命令可运行；demo 支持 happy-path/lost-response/worker-restart/inspect-refund。

- [x] 补齐查询分页、身份可见范围、统一错误结构、幂等重放响应头。
- [x] SSE 按 task 行锁产生的 seq 读取，先取状态和 high-water H，再补读至 H；终态追平才关闭。
- [x] 实现心跳、每批 100、无长事务、Last-Event-ID 参数校验和断开清理。
- [x] 写两个事务交错提交与终态竞争测试；SSE 用真实 Uvicorn TCP 服务和 httpx.stream 验收心跳/断连，不能只用 ASGITransport；给客户端设置明确终止条件，避免测试永久等待。
- [x] demo 创建唯一测试订单并打印关键 ID；故障模式隔离于普通服务；inspect-refund 只能 GET。
- [x] 干净测试环境运行迁移、seed、三个常驻应用角色；检查重复 seed 保留已退款订单。
- [x] 运行 `uv run pytest tests/integration/test_api_access.py tests/integration/test_events.py tests/e2e/test_demo.py -q`。

```python
@pytest.mark.asyncio
async def test_reviewer_cannot_submit_customer_task(reviewer):
    response = await reviewer.post("/v1/tasks", json={
        "order_id": "ORD-1001", "message": "申请退款"
    }, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 403
```

**完成标准：** A20、A22、完整鉴权通过，三条演示产生可阅读证据。Swagger 自带页面足够，不自行开发仪表盘。

### 任务 8：真实 Provider、固定评测和交付文档

**文件：** 新建 `src/aftercare/agent/http_provider.py`、`tests/unit/test_http_provider.py`、`tests/fixtures/tickets.jsonl`、`scripts/evaluate.py`、`docs/references.md`、`docs/test-results.md`；完善 README.md、.env.example、配置验证。

**输入：** 任务 4 的 ModelProvider 契约、设计第 5.2、13.4、16 节。\
**输出：** 可选真实模型接入、20 条固定评测、真实测试结果、来源归属及部署说明。

- [x] 使用 httpx 实现工具调用兼容适配器，模型地址只来自环境。请求关闭并行工具调用；一个响应多调用、格式不符、未知工具进入有界纠正。
- [x] 使用 MockTransport 验证消息序列：assistant tool_call、对应 tool result、后续请求；验证 429/5xx/timeout 重试预算由 runtime 统一控制。
- [x] 编写 20 条固定样本及期望结果；Mock 与真实模型分别生成报告，测试样本本身不由被测模型动态生成。
- [x] evaluate 输出 JSON/Markdown：模式、模型、版本、样本数、成功/错误分类、请求数、token（不可得则 null）、耗时。
- [x] 有真实模型凭据时运行端到端评测；无凭据时只完成适配器契约测试，并明确真实模型未验证。
- [x] 完成来源表，写清 nanobot/其他框架仅借鉴部分、本项目局限及不保证全局 exactly-once。
- [x] 运行 `uv run ruff check .`、`uv run pytest tests/unit tests/integration tests/e2e -q`；记录实际命令、日期、通过数量及失败/跳过原因。

**完成标准：** 设计 MVP 和 A01～A22 逐项有对应代码/测试；不能只交 happy path。真实模型缺凭据不妨碍离线版本交付，但不得声称真实业务准确率。

## 5. 测试覆盖映射

| 设计验收 | 实施任务 | 主测试文件 |
| --- | --- | --- |
| A01、A10～A18 | 2、6 | test_merchant_refunds.py、test_refund_executor.py、test_refund_crash_windows.py |
| A02、A05、A06、A15 | 5 | test_approvals.py |
| A03、A19 | 4、8 | test_policy.py、test_agent_contracts.py、test_investigation.py、test_http_provider.py |
| A04 | 3 | test_task_commands.py |
| A07～A09 | 3、4、6 | test_leases.py、test_checkpoints.py、test_refund_crash_windows.py |
| A20 | 7 | test_events.py |
| A21 | 3、6 | test_checkpoints.py、test_refund_executor.py |
| A22 | 1、2、7 | test_schema.py、test_merchant_refunds.py、test_demo.py |

## 6. 执行纪律与最终交接

- 每项先写关键行为测试，确认能捕获未实现/错误行为，再实现对应逻辑；不要给每个 getter 写形式化覆盖测试。
- 任务依赖按 1→2→3→4→5→6→7→8。可在主功能稳定后并行编写互不修改共享状态的测试与文档；不要让多个 Agent 同时更改状态协议或迁移。
- 完成一个任务后运行本任务检查；出现新改动或失败才重复相关检查，最终执行完整验收。
- 遇到冲突先查设计中明确规则。普通实现选择自行决定；涉及业务范围、授权边界或无法解决的环境阻塞，再向用户说明具体问题。
- 若已有 Git 仓库并获准提交，按里程碑做小提交；当前仅文档阶段不初始化 Git、不创建 commit、不推送远程。
- 测试通过不是可以自动上线或接真实支付的授权。本项目到本地可运行的模拟服务为止。

最终交付应包含：运行地址、已实现功能、真实执行的测试及数量、三条演示命令、真实模型验证状态、重要局限、来源说明。保持简洁；详细证据存放 docs/test-results.md。

## 7. 给下一次 Codex 会话的执行指令

以下指令仅在用户审阅并决定开始实现后使用：

```text
请先阅读：
docs/superpowers/specs/2026-09-18-aftercare-agent-design.md
docs/superpowers/plans/2026-09-18-aftercare-agent-implementation.md

我已审阅并批准这两份文档的 MVP 范围，请在当前仓库实施。
始终用中文沟通。按计划逐项实现、测试、更新文档，完成本地可运行版本。
不要增加真实支付、前端、Redis、多 Agent 或 RAG。
重点验证审批版本绑定、租约失效、HTTP 超时后的原键对账和同订单防重。
模型与只读工具允许有界重试，退款效果必须由商家账本和唯一约束保护。
不要把检查点、内存锁或 SKIP LOCKED 单独当成外部副作用幂等保证。
没有真实模型 Key 时完成 Mock 演示与适配器测试，并如实列出未验证部分。
不推送远程、不公网部署；对正常可逆的实现步骤无需反复请求许可。
```
