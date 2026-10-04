# 源码发布规则

## Git 内容边界

提交 src、tests、scripts、migrations、docker、文档、Compose、Dockerfile、pyproject.toml 和 uv.lock。保留设计与测试证据；评测样本和报告只含模拟订单及随机测试 ID。

不提交 .env、密钥文件、缓存、虚拟环境、日志、数据库、IDE 配置或打包产物。.env.example 只含明确标注的本地演示值，MODEL_API_KEY 为空。Git 提交使用 GitHub noreply 邮箱。

现有 Python 包按职责分层，无需移动业务模块来整理发布。英文入口 README.md，中文入口 README_ZH.md，版本记录 CHANGELOG.md，许可证 LICENSE。开发计划与本地执行记录保留在本地，不进入发布树；面向使用者的架构、运行与验收说明保留。

## 发布检查

```powershell
uv run ruff check .
uv run pytest tests/release -q
uv run python scripts/check_release.py
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml run --rm --build tests
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml down
```

check_release.py 检查 Git 索引中的内容（干净 checkout 时与 HEAD 相同），只输出可疑文件、行号和类别，不输出疑似密钥值。检查涵盖常见 Token、私钥、带凭据的 URL、本机用户路径、邮件地址及敏感文件名。显式演示凭据与官方/参考资料链接需要人工结合上下文复核。规则检查不能证明不存在所有形式的秘密。

普通文件仍限制为 2,000,000 字节，未知二进制仍需人工审查。项目说明图 `docs/images/cartoon-infographic.png` 经画面、PNG 结构与元数据复核后，按精确路径及 SHA256 登记在 `REVIEWED_BINARY_SHA256`；只有与登记内容完全相同的文件可通过例外。替换、追加内容或移动路径均不能沿用原审查，图片更新时必须重新复核并登记新摘要，不能按图片扩展名批量放行。

`tests/release` 使用真实临时 Git 索引，验证图片审查、篡改检测、原有文件限制、秘密检测及输出脱敏。它在宿主环境执行，需要 Git 和仓库中的说明图；业务 Docker 测试镜像仍只执行 `tests/unit`、`tests/integration`、`tests/e2e`。CI 的 `release-check` 与 `test` 独立运行，文档文件检查失败时也能获得完整业务测试结果；两个任务都通过才算 CI 通过。

发布时先审查 git diff --cached，再提交、创建版本标签，从标签生成源码 ZIP。归档与 SHA256 校验文件放在被忽略的 dist 中并作为 GitHub Release 附件上传，不提交生成物。

## 当前发行版

v0.1.0，MIT。版本号沿用项目现有 0.1.0；不把首个离线 MVP 标为生产级 1.0。仅模拟退款。真实模型端到端未验证，生产部署和性能负载尚未验收。
