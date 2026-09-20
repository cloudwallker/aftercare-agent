# 来源与依赖核查

核查日期：2026-09-18。实际依赖版本以 `uv.lock` 为准，生产依赖与 dev 测试依赖分离。

- [SQLAlchemy 2 PostgreSQL/psycopg 文档](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg)：使用 psycopg 3 方言及异步 engine，连接隔离显式设为 READ COMMITTED。
- [Pydantic 2 模型文档](https://docs.pydantic.dev/latest/concepts/models/)：请求与模型动作拒绝额外字段，金额使用严格整数。
- [FastAPI 版本说明](https://fastapi.tiangolo.com/deployment/versions/)：在主版本范围解析依赖后通过锁文件固定精确版本，不额外强制 Starlette 版本。
- [PostgreSQL 16 事务隔离](https://www.postgresql.org/docs/16/transaction-iso.html)：并发协议采用真实事务；不以 SQLite 或内存锁代替验收。

设计文档第 2 节列出的 nanobot、LangGraph、DBOS 与 Stripe 文档仅作架构和协议参考。本阶段未复制这些项目的实现源码，也未安装其工作流运行时。

项目自身采用所有者授权的 MIT 许可证（见根目录 LICENSE）；本次未引入第三方源码，依赖精确版本见 uv.lock；分发时仍须保留各依赖许可证声明。后续若复制外部源码，须记录文件、版本及许可证并保留声明。

## 任务 7、8 来源补充（2026-09-20）

| 来源 | 本项目使用范围 | 实现与许可证边界 |
| --- | --- | --- |
| [OpenAI Function calling 官方指南](https://developers.openai.com/api/docs/guides/function-calling) | assistant tool_calls 与 tool_call_id 配对、parallel_tool_calls=false | 自行实现 httpx 适配器，未复制 SDK 或示例源码；实际服务调用未验证 |
| nanobot（链接见设计第 2 节） | 简洁工具循环与职责划分 | 仅概念参考，未复制或安装 |
| LangGraph、DBOS（链接见设计第 2 节） | checkpoint、持久执行和恢复边界 | 未引入框架运行时或源码 |
| Stripe（链接见设计第 2 节） | 幂等键与结果不确定时的查询思想 | 模拟商家协议为本项目实现，不接入真实支付 |
| PostgreSQL、SQLAlchemy、Pydantic、FastAPI 官方文档（上列） | 事务、类型验证与 API | 安装依赖版本和哈希记录在 uv.lock |

依赖包随发行版提供的 LICENSE/元数据是其许可证依据；没有将任何外部项目许可证自动套用于本项目。项目自身由所有者指定为 MIT 许可证。本实现不宣称全局 exactly-once，协议边界与真实模型限制见 runtime.md 和 demo.md。
