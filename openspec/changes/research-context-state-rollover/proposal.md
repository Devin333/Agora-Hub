## Why

Research 长任务需要跨多轮检索、补证和模型上下文窗口继续执行；仅保留对话摘要可能丢失实验条件，重复携带原文或逐条反复核验又会放大 token 成本。现有 Harness 已提供受控上下文压缩、状态恢复和证据模型，本变更将其组合为“首次阅读记录条件、版本化状态复用、按需回读、预算内核验”的真实 Paper RAG 路径。

## What Changes

- 为单论文 Paper RAG 多轮补证引入可恢复的研究工作状态，复用现有 claim、evidence、source lineage 和 RAG session 模型；模型仅提出语义更新候选，Research 定义业务规则，Harness 控制验证与提交。
- 将当前上下文、研究状态、只追加历史、交接笔记与跨任务记忆分开；笔记从状态生成，原始材料通过带身份、版本、校验和的引用保存。
- 在下一次模型调用前按实际预算生成上下文投影，保护“结论、已知条件、核验范围、不确定性”的关联；达到切换策略时从已提交状态建立新窗口，不重置运行、预算、权限或工具事务。
- 对已记录条件执行确定性保全检查；对首次未记录的语义条件，通过有界、针对性的原文核对降低漏检风险。引用可解析、字段齐全和原文外存均不作为语义完整性的证明。
- 首次读取时共享证据完成抽取与范围核对；按新增推断、证据变化、冲突与最终关键结论触发增量回查，允许分组核验与有效结果复用，不默认逐条、逐轮新增 LLM 调用。
- 记录核验、摘要、回读、缓存与子任务的实际成本，设置调用及 token 上限；预算耗尽时保留待核验、证据不足或争议结果，限制相关结论使用。
- 对启用此策略的路径建立归档后裁剪、提交一致性、失效传播、恢复及纯 replay 验收；来源原文无法恢复的历史不得升级为已核验事实。

## Capabilities

### New Capabilities

- `research-working-state`: 有版本、来源及适用范围的研究状态，受控候选补丁、依赖失效和可恢复提交。
- `harness-context-window-rollover`: 从已提交状态组装预算内上下文，保护已记录条件，保留外部历史并在安全边界切换窗口。
- `research-evidence-recheck-policy`: 按需回查、冲突裁决、核验复用、批量核验、成本预算与关键结论准入。

### Modified Capabilities

无。本期为显式启用的新 Research 策略建立附加契约，复用现有 Harness 压缩与 Graph 约束，不修改未接入调用方的既有要求。启用路径必须在任何已有 conversation compaction 之前完成可核对的独立归档；全局重写 conversation store 和迁移所有旧调用方不在本期范围。

## Impact

- 首期生产场景：单论文 Paper RAG 的多轮检索、补证和最终回答；并行论文分析、Reader Repair 的业务接入作为后续范围，本期仍验证过期候选不会覆盖新状态。
- 主要接入位置：`backend/research/domain`、`backend/research/application/paper_rag_session.py`、`framework/harness/rag/session.py`、`framework/harness/context`、canonical durable event / artifact ports，以及 `infrastructure/research/context_runtime.py` 与 Research composition。
- 保持 `Research Graph + Graph Harness`、有界 `PLAN -> EXECUTE -> VERIFY`、确定性 gate、memory / publication authority 和 side-effect reconciliation；不引入新的 Workflow runtime。
- 不新增独立前端工作台或绕过 application service 的接口；在现有结果与诊断中体现核验范围、未决问题和成本。
- [prd.md](prd.md) 定义用户场景、范围、产品要求与验收；[design.md](design.md)、`specs/`、[tasks.md](tasks.md) 承接实现设计、规范和任务。当前交付为文档提案，代码与性能收益尚未实现或验证。
