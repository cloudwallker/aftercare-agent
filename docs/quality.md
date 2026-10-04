# 发布质量与 CI 验证

Aftercare Agent 是带人工审批、持久任务与幂等对账的模拟退款后端。质量验证分别覆盖业务协议和源码发布边界，退款、审批及 Worker 运行逻辑沿用 v0.1.0。

## 说明图发布缺陷

[原失败 CI](https://github.com/cloudwallker/aftercare-agent/actions/runs/36850392411) 中 Ruff 通过，但新增说明图为 6,061,349 字节，超过发布检查器对普通文件设置的 2,000,000 字节上限。检查因此失败，原串行工作流中的 Docker 业务测试被跳过。仅提高大小上限仍会触发未知二进制审查。

修复采用精确路径和已审查 SHA256 登记该说明图。画面、PNG 结构及来源元数据经过复核；内容变化必须重新审查。普通文件大小限制、敏感文件拒绝、未知二进制审查和秘密检测仍保留。发布规则见 [release.md](release.md)。

## 风险与自动化

| 风险 | 发布回归测试 |
| --- | --- |
| 已审查说明图被误拒绝 | 原路径、原内容通过 |
| 图片替换后沿用旧审查 | 追加或替换内容失败 |
| 图片例外扩散 | 原图换路径、未知 PNG 失败 |
| 原安全规则失效 | 超大文本、`.env`、模拟 token 仍被拒绝 |
| 检查泄露疑似秘密 | 输出不包含匹配值 |
| 工作区掩盖暂存内容 | 双向验证工作区与 Git 索引不一致 |
| 空仓库误判通过 | 空索引失败 |

`tests/release` 使用真实临时 Git 仓库，共 10 条回归。修复前 4 failed、6 passed，修复后 10 passed；宿主与原有 85 条业务单元测试合计 95 passed。以上结果不代替需要真实 PostgreSQL 的业务集成和端到端验证。

## 可复现检查

```powershell
uv sync --locked
uv run ruff check .
uv run pytest tests/unit tests/release -q
uv run python scripts/check_release.py
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml run --rm --build tests
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml down
```

宿主发布回归需要 Git 和仓库说明图。Docker 测试使用独立 `aftercare_test` 数据库，只执行 `tests/unit`、`tests/integration`、`tests/e2e`；发布回归不增加容器的 Git 或文档依赖。

CI 的 `release-check` 与 `test` 独立运行，两个任务均成功才算通过。上传后的完整结果以本次提交的 [GitHub Actions](https://github.com/cloudwallker/aftercare-agent/actions) 为准；原 v0.1.0 的 236 条完整业务测试记录见 [test-results.md](test-results.md)，不得用历史结果代替当前提交验证。真实模型、性能负载与生产部署尚未验收。
