# v0.1.0 发布审查记录

日期：2026-09-20。用户授权新建 GitHub 仓库、上传源码，并指定 MIT 许可证。

## 内容与隐私检查

- 从 Git 暂存树导出源码包，不打包整个工作目录。
- .venv、.cache、__pycache__、本地环境文件、数据库、日志、IDE 配置与密钥文件均排除。
- 初次扫描 97 个待提交文件；16 个提示逐一复核，全部为扫描器正则自身及单元测试中的 demo/admin/p/x/secret 等占位值。对明确测试文件和占位值增加有限例外后，扫描无发现。
- 人工复核 .env.example、Compose、数据库初始化、配置测试及文档：未发现真实 API Key、访问 Token、私钥、本机用户目录或个人邮箱。MODEL_API_KEY 示例为空；已有本地演示凭据不是秘密，不能用于公开生产部署。
- 评测和演示记录中的订单、任务 UUID、退款 ID 为模拟测试数据，不含真实客户资料。
- Git 作者采用 cloudwallker 与 GitHub noreply 邮箱。GitHub 认证通过现有 Git 凭据管理器完成，不将凭据写入源码、Git remote URL 或发布附件。
- MIT 授权由用户明确选择。第三方依赖许可证不受本项目 LICENSE 替代。

规则扫描与人工复核未发现真实秘密，但不能保证识别所有未知格式的敏感信息。

## 验证

| 检查 | 结果 |
| --- | --- |
| Ruff | All checks passed |
| Python 编译检查 | 通过 |
| Git 暂存差异空白检查 | 通过；整理了 SQL 行尾空白及 Markdown 换行 |
| 暂存树源码包构建 | 成功 |
| 从干净源码包执行完整 Docker/PostgreSQL 测试 | **236 passed in 65.37s** |
| 先前完整业务验收 | 见 test-results.md：三条演示、干净部署及 Mock 20/20 |
| 真实模型端到端 | 未配置凭据，未验证 |

发布形式为 v0.1.0 源码 ZIP 与 SHA256SUMS.txt，包含 Docker 配置、迁移、演示脚本、测试、文档和 MIT 许可证。没有宣称生产可用或真实支付能力。GitHub Actions 的实际运行结果应以仓库页面为准。
