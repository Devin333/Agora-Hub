## 1. Freeze baseline and contracts

- [ ] 1.1 固定当前 QASPER Dev baseline 的 dataset、corpus、chunk、embedding cache、代码 commit 和结果摘要。
- [ ] 1.2 定义 experiment manifest、case result、mapping result、failure record 和 report artifact schema。
- [ ] 1.3 为 Strict/Equivalent metric、mapping 和 execution profile 分配版本并加入兼容性校验。

## 2. Evidence mapping and scoring

- [ ] 2.1 实现 gold evidence 到 exact child、parent closure、相邻 chunk 和 normalized text span 的确定性映射。
- [ ] 2.2 实现 `StrictHit@K`、`EquivalentHit@K` 及对应 Coverage/MRR/nDCG，保留分子、分母、metric version 和 mapping version。
- [ ] 2.3 增加 mapping 状态和 failure reason 的逐题输出，禁止动态重建 gold 或静默删除不可映射题。
- [ ] 2.4 增加父子块、文本重叠、重复 chunk 和多必要 evidence group 的测试样例。

## 3. Retrieval ablation and experiment profiles

- [ ] 3.1 增加 semantic-only、BM25-only、field-only、semantic+BM25、semantic+field、full-hybrid 和 multi-query profiles。
- [ ] 3.2 保存每个通道的候选、provenance、首次命中位置和新增 gold evidence 贡献。
- [ ] 3.3 支持 candidate limit、overfetch、RRF k、rerank threshold 和权重的显式 sweep manifest。
- [ ] 3.4 复用 embedding cache，确保同一 manifest 重跑不会隐式调用未声明的付费模型或修改生产状态。

## 4. Reports and diagnostics

- [ ] 4.1 输出总体 Strict/Equivalent 指标、Coverage、MRR、nDCG、有效分母和不可评测分母。
- [ ] 4.2 增加按 question type、paper、section/field、evidence count、mapping status 和 first-hit rank 的切片报告。
- [ ] 4.3 增加 candidate recall miss、fusion/rerank miss、mapping failure 和 source/index failure 的失败归因。
- [ ] 4.4 分开记录 fixture 与 production execution profile，以及 index/request/judge 成本和 P50/P95 延迟。

## 5. CLI and integration

- [ ] 5.1 为 `rag-eval` 增加显式的 mapping validate、ablation run、sweep、score 和 report 入口，保持旧命令兼容。
- [ ] 5.2 将评测配置通过 application service 注入，避免 CLI 直接访问 retriever、store 或 executor。
- [ ] 5.3 增加离线 fixture 和 QASPER 外部目录适配说明，不把原始数据和大缓存复制进仓库。

## 6. Verification and handoff

- [ ] 6.1 运行评测相关单元/集成测试、`python -m scripts.dev compile` 和范围匹配的 smoke。
- [ ] 6.2 运行 `openspec validate qasper-retrieval-evaluation-optimization --strict` 并修复所有问题。
- [ ] 6.3 完成至少七组通道消融和一轮参数 sweep，保存逐题结果与报告。
- [ ] 6.4 对高差异样本进行人工抽检，冻结 mapping overlap 阈值与 metric version。
- [ ] 6.5 更新 PRD、交接文档和使用说明；若要改变生产默认策略，另开变更，不在本变更中直接切换。
