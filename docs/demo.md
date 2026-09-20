# 演示与评测

要求 Python 3.12、uv、已运行的 Docker Desktop；在仓库根目录执行。默认模型为 Mock，所有退款均为模拟账本操作。

## 启动常驻服务

首次从 `.env.example` 创建 `.env`，已有配置不要覆盖：

```powershell
Copy-Item .env.example .env
docker compose run --rm --build migrate
docker compose run --rm --build seed
docker compose up -d --build --wait api worker
```

Worker 依赖商家，Compose 自动启动 PostgreSQL、商家、API 和 Worker。默认 API 为 http://127.0.0.1:8000/docs；业务请求使用 `.env` 中的客户或审核员 Bearer Token。重复 seed 不覆盖已有订单或退款历史。停止服务用 `docker compose down`，保留数据卷。

## 三条隔离演示

```powershell
uv run python scripts/demo.py --scenario happy-path
uv run python scripts/demo.py --scenario lost-response
uv run python scripts/demo.py --scenario worker-restart
```

脚本构建 tests 镜像，在独立 `aftercare-demo` project、测试库和随机前缀订单上运行真实 HTTP/进程测试。结束后自动关闭服务，保留卷和本次记录。不要同时运行同一 project 的演示。

| 场景 | 验证内容 |
| --- | --- |
| happy-path | 创建→三项调查→人工批准→退款；输出 task_id、approval_id、operation_key、refund_id 和 confirmed_count=1 |
| lost-response | 商家已提交但返回 503；执行器 GET 原 key 收敛到同一 refund_id，只有一条 confirmed |
| worker-restart | unknown 提交后 HTTP 前、商家成功后本地提交前两个窗口强杀；新进程按真实租约接管，输出 recovery_seconds，均只有一条 confirmed |

故障入口仅在测试组合根；普通服务请求不能启用。演示为了可复现会自动扮演审核员批准其创建的模拟任务，常驻业务 API 仍要求审核员显式批准。

## 只读核查

常驻 Worker 和商家运行时，将下面 UUID 替换为自己的任务 ID：

```powershell
uv run python scripts/demo.py --scenario inspect-refund --task-id <任务UUID>
```

默认使用 `aftercare` project；自定义实例加 `--project 名称`，测试配置实例另加 `--test`。输出本地冻结请求和商家原 key 查询结果，仅 GET，不发起退款、不修改计数或状态。manual_review 不会被该命令自动关闭。

## 固定评测

```powershell
uv run python scripts/evaluate.py --mode mock
```

固定数据为 `tests/fixtures/tickets.jsonl`：8 条合格退款、8 条违规退款请求、4 条查询。使用独立 `aftercare-eval` project，每条样本独立订单，自动批准本轮模拟方案并保存历史；结束后关闭服务。输出 [mock.json](evaluations/mock.json) 和 [mock.md](evaluations/mock.md)，包含数据/锁文件哈希及逐项结果。

这是协议基线：Mock 对退款请求提出建议，违规建议应由后端拦截。真实模型可能主动给出 no_action 而与该基线不一致；匹配率不是通用模型质量评分。报告中的 business_accuracy 仅表示固定预期结果匹配率。无 token 用量为 null，恢复耗时由故障演示单独测量。

HTTP 模型使用 Chat Completions 兼容接口。在当前 shell 安全设置 `MODEL_BASE_URL`（包括服务要求的 `/v1` 等前缀）、`MODEL_API_KEY`、`MODEL_NAME` 后执行：

```powershell
uv run python scripts/evaluate.py --mode http
```

真实调用可能产生模型服务费用；报告分别写入 `http.json`/`http.md`。缺少配置时退出码 2，并生成 [未验证记录](evaluations/http-unverified.json)，不回退 Mock。当前交付没有真实模型凭据，真实模型端到端未验证。常驻 Worker 要使用该配置，另设 `MODEL_MODE=http` 并重建 Worker 容器。

## SSE

用支持流式读取的 HTTP 客户端 GET `/v1/tasks/{id}/events`，带 `Authorization: Bearer ...`；恢复时加 `Last-Event-ID: 上次seq`。每秒轮询、每批最多 100、每 15 秒注释心跳；终态最后事件发完关闭。客户端以 seq 去重。详细协议见 [runtime.md](runtime.md)。

局限：本地模拟商家、固定退款规则、单一 HTTP 模型协议，无前端、真实支付或人工核查写回界面；副作用安全依赖商家幂等协议，不保证全局 exactly-once。
