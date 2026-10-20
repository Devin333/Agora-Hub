## Why

当前 QASPER Dev 评测已经可以跑通，但结果同时受到两类问题影响：一是检索候选池、混合召回、字段路由和 reranker 仍有可调空间；二是 QASPER 的 evidence 标注、项目 chunk 切分以及父块/子块 ID 之间没有完全对齐，导致“内容已经找到了但严格 ID 被判错”。在继续更换模型或增加调用预算之前，需要建立一套可复现、可解释、不会通过放宽口径虚增成绩的检索优化评测流程。

## What Changes

- 建立 QASPER retrieval optimization 的版本化评测协议，固定数据集、语料、chunk、embedding、检索策略和评测器身份。
- 同时输出 `StrictHit@K` 和 `EquivalentHit@K`，明确区分 ID 严格匹配与父子块/文本内容等价匹配。
- 增加冻结的 evidence mapping，支持 gold evidence 与 child chunk、parent chunk、相邻连续 chunk 以及文本 span 的确定性映射；禁止根据本次检索结果重建 gold。
- 增加检索通道消融：Dense、BM25、Field、Dense+BM25、Dense+Field、全量 Hybrid 及 Multi-query 组合。
- 增加候选池、overfetch、RRF `k`、rerank threshold 和权重配置的实验矩阵，并保存每次实验的 manifest、逐题结果和失败诊断。
- 增加按问题类型、证据数量、论文和字段的切片报告，识别是召回不足、排序靠后、映射失败还是数据不可评测。
- 保留当前生产默认策略和运行路径；实验配置通过显式 profile/manifest 注入，只有通过离线结果和人工审核后才允许另开变更调整生产默认值。
- 明确评测分母、不可映射题、float evidence、needs_review 和 system_failure 的状态，不得通过删除困难样本或把缺失值改成零来提升结果。
- 补充面向新会话的 PRD、实现交接文档、命令示例和验收清单。

## Capabilities

### New Capabilities

- `qasper-retrieval-evaluation-optimization`: QASPER 论文证据检索的 evidence mapping、双口径命中指标、检索消融、参数实验矩阵和可复现报告。

### Modified Capabilities

- 无。现阶段只新增评测与实验能力，不直接修改生产检索默认行为或已有 RAG 质量门禁。

## Impact

- 主要涉及 `backend/research/rag/evaluation/`、`backend/research/rag/retrieval/` 的评测适配边界、`interfaces/cli/commands/rag_eval.py`、数据集/报告工件和对应测试。
- 复用现有 Research RAG 检索通道、字段检索、BM25、RRF、reranker 和评分契约；不新增平行 evaluator、run/event store 或 production index。
- QASPER 原始数据、embedding cache 和实验结果默认位于仓库外的本地评测目录，不写入生产索引，不提交大文件和凭据。
- 实验可使用真实 embedding 和检索服务，但默认评测命令必须显式声明 corpus、index、embedding、profile 和输出目录；禁止隐式调用付费模型或修改生产业务状态。
