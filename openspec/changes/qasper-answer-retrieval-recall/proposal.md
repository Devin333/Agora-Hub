## Why

300 题 QASPER 端到端运行显示，最终上下文只覆盖严格 gold 证据的 32.1%，等价口径也只有 53.1%。回答服务当前虽然接收生产检索策略，但实际只组装了 chunk dense store，没有字段召回或 reranker，因此评测没有测到已有检索能力。现在需要把召回配置和真实 fixture 索引接入回答评测，先用确定性的 Strict/Equivalent 覆盖验证检索上限，再决定是否重跑完整回答。

## What Changes

- 为 QASPER answer evaluation 增加显式 retrieval components 配置，不修改生产 Research 默认策略。
- 让 answer evaluation 使用与缓存身份绑定的字段 embedding index；缓存缺少字段文本向量时明确失败，不静默退回 chunk-only。
- 在 composition 层注入已有的 reranker port，提供显式 lightweight reranker profile；application 层不依赖 infrastructure。CrossEncoder 的模型选择与评测留待后续独立实验，本变更不下载模型。
- 对启用的 retrieval policy 记录 overfetch、字段召回、reranker 和最终上下文预算，确保扩大候选池不会扩大送入模型的上下文上限。
- 增加离线预检、组件注入和候选预算测试，并提供固定来源运行样本的 retrieval-only replay 入口；对同一批 300 题分别报告候选池和最终上下文的 Strict/Equivalent coverage。
- 保持默认 policy 和现有 chunk-only 离线 retrieval evaluation 行为不变；不把 fixture 结果宣称为 Qdrant 生产质量。

## Capabilities

### New Capabilities

- `qasper-answer-retrieval-recall`: QASPER 端到端回答评测的显式召回组件、候选 overfetch、reranker/field index 注入、身份预检和可观测性。

### Modified Capabilities

无。现有 `rag-live-answer-evaluation` 和通用 paper retrieval 要求不改变；本变更只扩展 QASPER 评测 composition 的可选路径。

## Impact

- 修改 `backend/research/application/qasper_answer_evaluation.py`、`interfaces/composition/qasper_evaluation.py` 及 CLI，新增独立 replay application service、composition 和回放报告。
- 复用 `CachedFixtureFieldIndex`、`LightweightLexicalReranker`、`RetrievalPolicy` 和 `PaperRAGSession` 的现有接口。
- 增加 QASPER answer evaluation 单测和离线缓存完整性预检；不新增生产索引写入、业务 memory、active skill 或外部数据文件。
- 需要重新运行一组不调用回答模型的 retrieval-only coverage 对照；只有在组件身份、缓存完整性和候选预算验证通过后，才允许重跑 300 题回答评测。
