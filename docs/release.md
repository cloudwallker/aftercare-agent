# 源码发布规则

## Git 内容边界

提交 src、tests、scripts、migrations、docker、文档、Compose、Dockerfile、pyproject.toml 和 uv.lock。保留设计与测试证据；评测样本和报告只含模拟订单及随机测试 ID。

不提交 .env、密钥文件、缓存、虚拟环境、日志、数据库、IDE 配置或打包产物。.env.example 只含明确标注的本地演示值，MODEL_API_KEY 为空。Git 提交使用 GitHub noreply 邮箱。

现有 Python 包按职责分层，无需移动业务模块来整理发布。中文入口 README.md，英文入口 README.en.md，版本记录 CHANGELOG.md，许可证 LICENSE。

## 发布检查

```powershell
uv run ruff check .
uv run python scripts/check_release.py
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml run --rm --build tests
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml down
```

check_release.py 检查 Git 索引中的内容（干净 checkout 时与 HEAD 相同），只输出可疑文件、行号和类别，不输出疑似密钥值。检查涵盖常见 Token、私钥、带凭据的 URL、本机用户路径、邮件地址及敏感文件名。显式演示凭据与官方/参考资料链接需要人工结合上下文复核。规则检查不能证明不存在所有形式的秘密。

发布时先审查 git diff --cached，再提交、创建版本标签，从标签生成源码 ZIP。归档与 SHA256 校验文件放在被忽略的 dist 中并作为 GitHub Release 附件上传，不提交生成物。

## 当前发行版

v0.1.0，MIT。版本号沿用项目现有 0.1.0；不把首个离线 MVP 标为生产级 1.0。仅模拟退款。真实模型端到端未验证，生产部署和性能负载尚未验收。
