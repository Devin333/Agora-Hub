## Why

项目已经具备跨来源研究的搜索、证据和报告产物，但目前没有一条可重复的外部 benchmark 适配路径，无法用学术调研任务稳定测试“问题输入 → 多来源检索 → 带引用研究报告”的真实流程。需要先接入与项目的 `papers`/`projects` 来源模型相匹配的 ReportBench 输出合同，并在不调用付费模型的情况下验证任务、报告、引用和运行元数据的边界。

## What Changes

- 新增 ReportBench 适配能力，将项目跨来源研究结果规范化为 benchmark 所需的任务输出记录。
- 为报告正文、结构化来源、引用定位、运行状态、延迟和成本元数据建立严格的离线校验合同。
- 提供只读、可重复的 CLI，用于导入/校验 ReportBench 题目和导出项目运行结果；默认不启动模型、网页抓取或付费评审调用。
- 提供可注入的跨来源研究 runner 边界，使后续能够接入真实 Guided Research/报告运行路径，而不让 benchmark 代码直接依赖具体 UI、executor 或 storage 实现。
- 增加离线 fixture、命令帮助和使用文档，明确 `dry-run`、小批量和正式评测的边界；不把 benchmark 结果自动标记为生产质量或发布决策。

## Capabilities

### New Capabilities

- `cross-source-reportbench-adapter`: 跨来源研究报告到 ReportBench 的任务、输出、引用和运行记录适配合同。
- `cross-source-reportbench-cli`: ReportBench 题目/输出的离线校验、导入导出和后续 runner 接入口。

### Modified Capabilities

- 无

## Impact

- 影响 `backend/research/benchmark/`、`interfaces/cli/commands/`、`scripts/dev.py` 和对应测试/文档。
- 不修改生产检索、Guided Research、报告生成、质量 gate 或持久化路径；适配器通过端口接收结果。
- 不将 ReportBench 数据集或参考答案复制进生产索引；不新增必需的第三方运行时依赖。
- 外部 judge、网页抓取和真实 API 调用留在后续显式运行步骤，本变更默认保持离线。
