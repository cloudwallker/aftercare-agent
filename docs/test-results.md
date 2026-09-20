# 测试记录

最新结果（2026-09-20）：**任务 1～8 的 236 项测试全部通过**，标准 Docker / PostgreSQL：`236 passed in 63.62s`。三条演示 CLI、干净部署、只读核查均通过；固定 Mock 20/20，真实模型端到端未验证。下方保留历史，最新记录见文末。

日期：2026-09-18。范围：实施计划任务 1，非完整 MVP 验收。

## 实际执行

环境：Windows / PowerShell、Python 3.12.12、Docker Engine 29.0.1、Docker 官方 `postgres:16` 镜像。使用独立 `aftercare-test` project 与 `aftercare_test` 数据库。

| 检查 | 实际结果 |
| --- | --- |
| 初次单元测试 | 26 项失败，原因是 config/db/contracts 尚未实现 |
| Token 空白规范化回归 | 修复前 1 failed / 12 passed；修复后通过 |
| 空数据库迁移 smoke test | 1 failed，`public.alembic_version` 不存在，符合预期 |
| `python -m alembic upgrade head` | 成功，版本 `0001_initial` |
| 再次 `python -m alembic upgrade head` | 成功，同一 head 无新增迁移 |
| 首轮真实 PostgreSQL 全量测试 | 42 passed / 1 failed，测试 SQL 内嵌 JSON 被识别为绑定参数 |
| 修复后的全量测试 | **43 passed in 1.57s**（27 单元 + 16 集成） |
| `ruff check .` | All checks passed |
| `python -m compileall -q src migrations tests` | 成功 |
| `uv lock --check --offline` | 成功，33 packages |
| Docker 生产/测试目标构建 | 成功，生产镜像不含 dev 依赖 |
| 标准 Compose 测试 | **43 passed in 0.71s**，Linux Python 3.12.14 |

宿主验证命令（先将两个测试 URL 指向显式独立测试库）：

```powershell
.venv/Scripts/python -m pytest tests/unit tests/integration tests/e2e -q --tb=short -p no:cacheprovider
.venv/Scripts/ruff check .
```

使用 `-p no:cacheprovider` 是避免当前 Windows 提权与沙箱用户间的 pytest 缓存目录权限告警，不影响测试收集或数据库事务。缺少 TEST_* URL 的集成测试会失败，没有静默 skip。

最初 Docker Hub 下载出现 EOF，BuildKit 获取 Python 镜像认证 Token 连续超时。PostgreSQL 重试成功后，先通过 `.cache/compose.host-test.yaml` 为测试库临时分配 `127.0.0.1` 随机端口，使用宿主 Python 验证。随后 `docker pull python:3.12-slim` 成功，生产与测试镜像均构建成功。标准 Compose 测试再次通过，数据库恢复为不映射宿主端口。

```powershell
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml build tests migrate
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml run --rm tests
```

本轮宿主和容器各跑同一套 43 项测试，不累加声称 86 项不同测试。

## 覆盖与未覆盖

已验证 8 张业务表与迁移版本、app/merchant 双向权限隔离、版本表只读、两类部分唯一索引的实际重复插入、declined 释放订单占用、不存在订单的拒绝记录、审批和请求不可变、running 租约 CHECK、UTC、READ COMMITTED、业务角色非超级用户。

本轮未实现或验证：HTTP 商家账本、任务/审批 API、Worker 租约算法、Agent 调查、退款对账、SSE、进程强杀恢复、3 条演示、真实模型适配器及评测。`tests/e2e` 目前为空目录；43 项不是完整 A01～A22 的通过数量。真实模型端到端未验证。

## 锁定的主要版本

| 依赖 | 版本 |
| --- | --- |
| FastAPI | 0.141.1 |
| Pydantic | 2.13.5 |
| SQLAlchemy | 2.0.54 |
| psycopg / psycopg-binary | 3.3.5 |
| Alembic | 1.20.0 |
| httpx | 0.28.1 |
| pytest | 9.1.1 |
| pytest-asyncio | 1.4.0 |
| Ruff | 0.16.8 |
| uv | 0.9.8 |

完整直接与传递依赖见 `uv.lock`。所有业务订单与凭据均为本地模拟用途。

## 2026-09-20：任务 2 商家幂等账本

| 验证 | 结果 |
| --- | --- |
| 商家接口实现前 | 新增 3 项单元失败；24 项集成因接口模块尚不存在失败/报错，无 skip |
| 宿主 PostgreSQL 首轮完整测试 | 78 passed in 4.89s |
| seed 与 schema 专项 | 18 passed in 1.60s |
| 含斜杠 key 回归 | 修复前 POST 200 / GET 404；改路径参数后通过 |
| Windows 真实 TCP 进程测试 | 首次因默认 Proactor 循环与 psycopg 不兼容而就绪超时；显式 Selector 循环后通过 |
| Docker 全量单元/集成/端到端 | **80 passed in 4.68s**：33 单元 + 46 集成 + 1 TCP 端到端 |
| Ruff / Python 编译 | 通过 |
| 代码审查及修复复审 | 通过，关闭含斜杠 key 无法查询的问题 |
| Compose seed 连续执行 | 首次新增 6 条，第二次新增 0 条 |
| Compose merchant 启动 | `up -d --build --wait merchant` 成功，健康检查 Healthy，无宿主端口映射 |

实际 Docker 命令：

```powershell
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml build tests merchant seed migrate
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml run --rm --build tests
```

测试新增验证：同键同参原样重放、同键修改金额/订单/授权 ID 冲突、20 个并发同键/不同键请求仅一条 confirmed、订单版本仅增长一次、稳定拒绝及后续订单变化不改变原拒绝、原键 GET 无副作用、资格和归属校验、故障开关边界、账本更新异常时订单与 key 整体回滚。

seed 测试使用唯一前缀，覆盖 4 次并发初始化、已有订单修改不被覆盖、已退款种子账本稳定，以及真实 HTTP 退款后重跑不重置历史。提交事务测试使用业务角色，测试管理员仅在 teardown 按本测试登记的订单 ID 清理，且 URL 强制匹配同一 `_test` 库。

端到端测试启动真实 Uvicorn 子进程，第一次退款提交后返回 503，关闭该子进程后启动新进程，再查询和重放原键，验证同一 refund_id、仅一笔成功退款和持久化 fault_consumed。子进程只接收商家凭据；此用例不是后续 Worker 强杀故障验收，不能替代 A07/A11/A12。

当前覆盖 A10/A13/A14/A17 的商家部分和 A22 的 seed 部分。任务 3～8（应用任务、租约算法、审批、Agent、退款执行器、SSE 和完整演示）仍待实现，真实模型端到端仍未验证。

## 2026-09-20：任务 3 持久任务、租约与检查点

| 验证 | 结果 |
| --- | --- |
| 核心接口实现前 | 9 failed + 3 errors；实现后 12 passed |
| Worker 实现前后 | 实现前 4 failed / 12 passed；实现后 16 passed |
| 非法游标回归 | 修复前 1 failed / 3 passed；修复后统一返回 422 |
| 宿主阶段完整回归 | 107 passed in 16.22s |
| 补充边界后的 Docker 全套 | **110 passed in 9.16s**：37 单元 + 72 集成 + 1 TCP 端到端 |
| Ruff / Python 编译 | `ruff check .` 与 `python -m compileall -q src tests` 通过 |
| 代码审查 | 通过，未发现实质问题 |
| Compose API 启动 | `up -d --build --wait api` 成功，容器 Healthy |
| 真实 HTTP 只读检查 | `/health/ready` 返回 200 和 `{"status":"ready"}`；OpenAPI 包含任务及步骤接口 |

实际容器验证命令：

```powershell
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml run --rm --build tests
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml up -d --build --wait api
```

本任务新增 4 项单元测试和 26 项集成测试。宿主 107 项与最终 Docker 110 项是两个阶段的回归，不累计为 217 项不同测试。数据库采用独立 `aftercare_test`；业务写入使用应用角色，管理员仅按本测试随机客户清理数据。

已验证 20 个并发创建请求仅产生一个任务、原响应在终态后仍可重放、同键异参冲突、身份与可见性、游标分页；验证租约抢占、过期续租拒绝、旧 epoch 写回拒绝及延迟重试。步骤测试覆盖 pending 接管与尝试上限、成功结果复用、预扣预算和提交事件失败的事务回滚、模型/工具配额、本地规则不消耗 HTTP 尝试、无效输出次数持久化、剩余时间与输出/checkpoint 大小限制。

Worker 测试覆盖领取前并发槽位、独立心跳、handler 期间不持行锁、失租取消、未完成 handler 不伪报成功、停机取消且不继续领取。运行时接口与调用要求见 [runtime.md](runtime.md)。

本阶段覆盖 A04、A08、A09 的本地 fencing 和 A21 的命令/步骤/checkpoint/事件事务部分；审批与退款结果原子提交仍属于后续任务。尚未接入调查 Agent、审批、退款执行器、SSE 或常驻 Worker 服务；没有将协程取消测试等同于真实 Worker 进程强杀恢复，真实模型端到端也未验证。

验证结束后已执行 `docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml down`，本次测试容器及网络已关闭，数据库卷与镜像保留。

## 2026-09-20：任务 4 只读调查与待审批方案

| 验证 | 实际结果 |
| --- | --- |
| 宿主单元测试 | 65 passed in 0.85s |
| 首轮 Docker 全套 | 156 passed in 14.76s |
| 补齐恢复与超时边界后的 Docker 全套 | **163 passed in 24.33s**：65 单元 + 97 集成 + 1 商家 TCP 端到端 |
| 任务 4 新增覆盖 | 28 项单元测试、25 项真实 PostgreSQL 集成测试 |
| Ruff / Python 编译 | `ruff check .`、`python -m compileall -q src tests` 通过 |

完整回归命令仍为：

```powershell
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml run --rm --build tests
```

规则测试覆盖金额整数类型及上限、币种、支付状态、退款历史、延误阈值、客户归属与快照版本。动作契约拒绝非法 JSON、越权工具、非空工具参数、金额覆盖、重复证据和超长说明。HTTP 工具覆盖脱敏错误、429/503 可重试及输出大小限制。

调查测试验证合格方案的金额、版本、证据、规范哈希与 24 小时有效期；模型建议不能绕过规则；no_action 仍完成三类查询。非法输出与伪证据仅可纠正一次，纠正次数跨恢复保留；跨工单证据拒绝。已保存动作、pending 工具和成功工具均按检查点恢复，不重新选择或重复成功查询。审批事件失败时 approval、task 状态整体回滚，重试复用最终建议。

预算测试覆盖无限工具循环、模型及远程工具失败后同一步骤重试、持久化 1/3 秒退避、模型硬超时、本代 180 秒上限。快照版本不一致先反馈并允许重新查询；恢复一致可生成审批，持续不一致在预算耗尽时为 SNAPSHOT_UNSTABLE。代码复核中将该错误归类限定为当前快照仍不一致，避免历史不一致掩盖后续非法输出错误。

所有调查集成测试只允许商家 GET，且断言不创建 refund_operation。当前覆盖 A03、A19 和 A07 的步骤恢复部分；任务 5 的审批决定、任务 6 的执行对账、SSE 与真实 Worker 强杀恢复仍待实现。MockProvider 是确定性演示，真实模型端到端未验证。首轮与最终回归测试数不重复累加。

独立服务验收：`docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml up -d --build --wait api worker` 成功。使用随机前缀 seed 的模拟订单，通过真实 API POST 创建工单并轮询详情，独立 Worker 通过商家 HTTP 调查：ORD-1001 → waiting_approval（金额 19900 分）；ORD-1003 → rejected/POLICY_DENIED；ORD-1002 → succeeded/no_action。直接查询商家账本确认这三笔订单的退款记录为 0。该额外 smoke 验证不计入 163 项 pytest 数量，也不代替完整审批退款端到端验收；随机前缀订单及任务历史保留。

验收结束后执行 Compose `down` 成功，API、Worker、商家、迁移和数据库测试容器及网络已关闭；测试卷和镜像保留。

## 2026-09-20：任务 5、6 审批与退款对账

| 验证 | 实际结果 |
| --- | --- |
| 首轮数据库完整回归 | 185 passed in 21.49s |
| 最终宿主单元测试 | 74 passed in 1.00s |
| 最终 Docker 完整回归 | **202 passed in 31.93s**：74 单元 + 125 集成 + 3 TCP/进程端到端 |
| 任务 5、6 新增覆盖 | 9 单元、28 集成、2 真实 Worker 强杀恢复测试 |
| Ruff / 编译 | `ruff check .`、`python -m compileall -q src tests` 通过 |

完整回归命令：

```powershell
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml run --rm --build tests
```

审批测试覆盖 20 个同键并发决定、20 个不同键及相反决定竞争、批准/重新调查竞争、终态后决定重放、审批拒绝不产生 operation、旧审批失效、最大三代、角色和哈希拒绝、审批过期。等待时间超过 180 秒后重新调查会获得新预算，旧审批仍保留供查询。命令/审批/任务/事件失败时整体回滚。

退款测试覆盖完整 API 创建→调查→并发批准→Worker 退款；商家提交后丢失响应，后续 GET 复用同一 refund_id 且不再 POST；第三次 POST 成功但响应丢失后仍用剩余 GET 恢复。两个任务争同一订单仅一个 operation，另一个 ORDER_REFUND_RESERVED。批准后订单版本变化得到确定 ORDER_CHANGED，不产生成功退款。

授权到期测试区分冻结之前和之后；扫描跳过 running 及有 operation 的 queued。持锁后重新读取 operation，防止扫描语句旧快照误作废新冻结的授权。未知结果测试覆盖持续不可用、missing、指纹不符、八次 GET/三次 POST 上限、最后一次 GET missing 禁止再 POST、过期 deadline 不发送 HTTP、冻结后异常进入 manual_review。操作保持 prepared/unknown，订单占用不释放。

事务回滚测试验证冻结事件失败不留下 operation；商家已成功但本地终态事件失败时 operation 仍 unknown、任务仍 running，恢复通过 GET 补交。旧 Worker 在 HTTP 前或成功响应后暂停并被接管，其本地写回得到 LeaseLost；外部旧请求只重放原退款，确认账本仍只有一条。

两个进程故障测试启动自己持有的真实 Uvicorn 商家与 Worker 子进程，在 `before_refund_http` 和 `after_refund_http_before_commit` Hook 写出 PID/任务信号后强杀指定 Worker。新 Worker 等待真实两秒测试租约过期再接管；校验 epoch 增加、operation_key 不变、任务 succeeded、商家仅一条 confirmed 且 refund_id 一致。前者 dispatch_count=2（首次预扣但未发送），后者 dispatch_count=1（恢复只 GET）。测试只向 Worker 传应用连接、向商家传商家连接；管理员凭据不传入业务子进程，全部子进程在 teardown 回收。

本阶段验证 A01、A02、A05、A06、A09 的外部竞态、A10～A18 及 A21 的结果提交部分；A14 等既有商家测试持续通过。A07 的调查步骤恢复已做集成验证，但调查阶段真实强杀仍未新增；SSE/完整演示脚本与真实模型评测仍待任务 7、8。202 是最终测试集合，不与前序回归数量重复累加。

标准服务 smoke 额外验收通过：`up -d --build --wait api worker` 启动实际 API、商家和 Worker；新随机前缀订单经真实 HTTP 创建工单、查询待审批、审核员批准后进入 succeeded/refunded。直接读取商家账本确认仅一条 confirmed，refund_id 与任务结果相同；再次提交相同审批命令仍原样重放，返回 Idempotency-Replayed。此额外检查不计入 202 项自动测试数，测试订单及历史保留。

任务 5、6 验收后 Compose `down` 成功，本次 API、Worker、商家、迁移、数据库容器和网络已关闭；测试卷与镜像保留。

## 2026-09-20：任务 7、8 最终验收

### 实际命令与结果

| 命令/阶段 | 实际结果 |
| --- | --- |
| 新 SSE/HTTP Provider 测试实现前收集 | 2 collection errors，目标接口尚未实现；后续已修复 |
| 首轮 HTTP Provider 单元测试 | 10 passed in 0.86s |
| 首轮 Docker 全套 | 227 passed in 66.37s |
| `docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml run --rm --build tests` | **236 passed in 63.62s**，无失败或跳过 |
| `python scripts/demo.py --scenario happy-path` | 1 passed in 3.64s，confirmed_count=1 |
| `python scripts/demo.py --scenario lost-response` | 1 passed in 5.96s，503 后 GET 原 key 收敛，confirmed_count=1 |
| `python scripts/demo.py --scenario worker-restart` | 2 passed in 8.05s；两个强杀窗口恢复分别 2.229/2.180 秒，各一条 confirmed |
| `python scripts/evaluate.py --mode mock` | 20/20，80 次 Mock 调用，8 条违规建议被拦截，重复退款 0，总耗时 5.555 秒，tokens=null |
| HTTP 评测 | 未运行外部模型；缺少配置时明确拒绝并生成 http-unverified.json，不回退 Mock |
| `.venv/Scripts/ruff check .` | All checks passed |
| `.venv/Scripts/python -m compileall -q src scripts tests migrations` | 退出码 0 |

宿主脚本使用 `.venv/Scripts/python`，与指南 `uv run python` 指向同一锁定环境。全套包含真实 PostgreSQL、15 秒 SSE 心跳/断连续传、调查步骤强杀和退款两个强杀窗口。HTTPProvider 的 MockTransport 契约测试不等于真实模型端到端测试。

新增覆盖跨客户四类读取 404、无效事件游标、107 条终态事件分页、两事务交错、HTTP 消息配对/超时/非法输出、真实 Provider 契约进入统一调查 runtime、只读核查不改状态和计数。修正订单不存在时的 rejected 业务结论，以及退款 POST 的确定 404 拒绝解析。

### 干净 Compose 部署

独立 `aftercare-delivery-test` project 首次创建全新卷，运行 `run --rm --build seed`（自动迁移）、再次 `run --rm seed`，分别新增 6/0 个订单；`up -d --build --wait api worker` 后 PostgreSQL、API、商家和 Worker 均健康。

真实 HTTP 创建→调查→批准→退款成功，任务 `5277648a-b5b2-47d6-ad58-1d521492cb52`，商家 refund_id `23cad7bd-850b-4fdd-8627-cdef3ae8a52a`，成功账本仅一条；终态审批幂等重放及 SSE 最后事件均通过。

`python scripts/demo.py --scenario inspect-refund --project aftercare-delivery-test --test --task-id 5277648a-b5b2-47d6-ad58-1d521492cb52` 成功返回本地 confirmed 与相同原 key 的商家 confirmed。核查无 POST，不修改状态。重复 seed 保留已退款订单由 test_seed.py 的真实 HTTP 用例验证。

本轮演示和评测自动关闭服务；回归与干净部署服务也在交付前关闭，保留数据卷，不初始化 Git 或创建提交。

### 评测解释与交付范围

[Mock JSON](evaluations/mock.json)、[Mock Markdown](evaluations/mock.md)、[HTTP 未验证记录](evaluations/http-unverified.json)。20 条固定样本与数据哈希可复查；8 条违规退款请求由 Mock 提出建议，再由确定性规则拒绝。匹配率只验证该协议基线，不代表真实模型准确率；模型主动拒绝建议也可能不匹配本基线。无模型 token 信息和本评测未注入故障的恢复耗时均记 null，真实故障恢复耗时见上方演示。

[A01～A22](acceptance.md) 均有对应代码/测试。[演示指南](demo.md) 提供部署、三场景、SSE、只读核查及 HTTP 配置。真实支付、生产规模和真实模型效果不在本轮已验证范围。
