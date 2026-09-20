# mock 固定协议评测

固定协议基线：Mock按请求意图提出建议，违规建议由规则拦截；并非真实模型能力评分。恢复耗时见 worker-restart 演示。

模型：MockProvider；程序版本：0.1.0；日期：2026-09-20T04:58:28.663129+00:00。
总耗时：5.555 秒；违规建议拦截：8。
错误分类：{"POLICY_DENIED": 8}。

样本 20；结果匹配 20；重复退款 0；模型请求 80；token None。

| 样本 | 预期 | 实际 | 调用数 | 秒 |
| --- | --- | --- | --- | --- |
| R01 | refunded | refunded | 4 | 0.27 |
| R02 | refunded | refunded | 4 | 0.23 |
| R03 | refunded | refunded | 4 | 0.245 |
| R04 | refunded | refunded | 4 | 0.266 |
| R05 | refunded | refunded | 4 | 0.316 |
| R06 | refunded | refunded | 4 | 0.389 |
| R07 | refunded | refunded | 4 | 0.303 |
| R08 | refunded | refunded | 4 | 0.228 |
| D01 | rejected | rejected | 4 | 0.163 |
| D02 | rejected | rejected | 4 | 0.166 |
| D03 | rejected | rejected | 4 | 0.159 |
| D04 | rejected | rejected | 4 | 0.15 |
| D05 | rejected | rejected | 4 | 0.156 |
| D06 | rejected | rejected | 4 | 0.158 |
| D07 | rejected | rejected | 4 | 0.142 |
| D08 | rejected | rejected | 4 | 0.135 |
| Q01 | no_action | no_action | 4 | 0.148 |
| Q02 | no_action | no_action | 4 | 0.131 |
| Q03 | no_action | no_action | 4 | 0.155 |
| Q04 | no_action | no_action | 4 | 0.156 |
