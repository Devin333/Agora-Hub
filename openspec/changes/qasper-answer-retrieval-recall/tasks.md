## 1. 检索 profile 契约

- [x] 1.1 新增不可变 QASPER answer retrieval profile，明确策略、可选字段索引/reranker、执行方式和最终上下文预算。
- [x] 1.2 新增确定性 profile 身份和 manifest 序列化，报告策略设置、组件名称、缓存/索引身份及 fixture 边界。

## 2. 回答评测组件接入

- [x] 2.1 为 `QasperAnswerEvaluationService` 接入可选字段索引、基础 reranker 和字段 reranker ports，保留原 chunk-only 构造行为。
- [x] 2.2 仅在 composition 构建 `CachedFixtureFieldIndex` 和 `LightweightLexicalReranker`，按显式策略启用，application 不导入 infrastructure。
- [x] 2.3 首题前预检字段缓存完整性并提供失败诊断；选择字段 profile 后不静默回退到 chunk-only。
- [x] 2.4 回答报告记录组件可用性、overfetch、rerank 实际调用及最终上下文预算。

## 3. 确定性检索回放

- [x] 3.1 新增 retrieval-only replay service/CLI，复用固定 QASPER 样本和冻结映射，不调用回答模型或 judge。
- [x] 3.2 实现 baseline 和候选扩展 profiles，固定分母，报告 strict/equivalent 候选及上下文覆盖、候选 provenance 和失败。
- [x] 3.3 显式执行 reranker profile，遵守 policy intent gates 并记录各 profile 的实际调用。

## 4. 验证与证据

- [x] 4.1 测试默认兼容、profile 身份、字段缓存预检、reranker policy gates、实际候选扩展和 8 条最终上下文预算。
- [x] 4.2 使用 fake ports 和冻结映射 fixture 测试确定性 replay，覆盖身份不匹配、初始与运行时向量缺失等阻断情形。
- [x] 4.3 运行 QASPER 测试、`openspec validate qasper-answer-retrieval-recall --strict`、`python -m scripts.dev compile` 和 `python -m scripts.dev smoke`，提交前修复失败根因。
- [x] 4.4 在独立外部目录完成确定性 coverage replay，记录 manifest/report 并核验重复运行，不覆盖原 300 题回答工件。
