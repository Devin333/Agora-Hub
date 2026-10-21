## 1. Freeze baseline and contracts

- [x] 1.1 固定当前 QASPER Dev baseline 的 dataset、corpus、chunk、embedding cache、代码 commit 和结果摘要。
- [x] 1.2 定义 experiment manifest、case result、mapping result、failure record 和 report artifact schema。
- [x] 1.3 为 Strict/Equivalent metric、mapping 和 execution profile 分配版本并加入兼容性校验。

## 2. Evidence mapping and scoring

- [x] 2.1 实现 gold evidence 到 exact child、parent closure、相邻 chunk 和 normalized text span 的确定性映射。
- [x] 2.2 实现 `StrictHit@K`、`EquivalentHit@K` 及对应 Coverage/MRR/nDCG，保留分子、分母、metric version 和 mapping version。
- [x] 2.3 增加 mapping 状态和 failure reason 的逐题输出，禁止动态重建 gold 或静默删除不可映射题。
- [x] 2.4 增加父子块、文本重叠、重复 chunk 和多必要 evidence group 的测试样例。

## 3. Retrieval ablation and experiment profiles

- [x] 3.1 增加 semantic-only、BM25-only、field-only、semantic+BM25、semantic+field、full-hybrid 和 multi-query profiles。
- [x] 3.2 保存每个通道的候选、provenance、首次命中位置和新增 gold evidence 贡献。
- [x] 3.3 支持 candidate limit、overfetch、RRF k、rerank threshold 和权重的显式 sweep manifest。
- [x] 3.4 复用 embedding cache，确保同一 manifest 重跑不会隐式调用未声明的付费模型或修改生产状态。

## 4. Reports and diagnostics

- [x] 4.1 输出总体 Strict/Equivalent 指标、Coverage、MRR、nDCG、有效分母和不可评测分母。
- [x] 4.2 增加按 question type、paper、section/field、evidence count、mapping status 和 first-hit rank 的切片报告。
- [x] 4.3 增加 candidate recall miss、fusion/rerank miss、mapping failure 和 source/index failure 的失败归因。
- [x] 4.4 分开记录 fixture 与 production execution profile，以及 index/request/judge 成本和 P50/P95 延迟。

## 5. CLI and integration

- [x] 5.1 为 `rag-eval` 增加显式的 mapping validate、ablation run、sweep、score 和 report 入口，保持旧命令兼容。
- [x] 5.2 将评测配置通过 application service 注入，避免 CLI 直接访问 retriever、store 或 executor。
- [x] 5.3 增加离线 fixture 和 QASPER 外部目录适配说明，不把原始数据和大缓存复制进仓库。

## 6. Verification and handoff

- [x] 6.1 运行评测相关单元/集成测试、`python -m scripts.dev compile` 和范围匹配的 smoke。
- [x] 6.2 运行 `openspec validate qasper-retrieval-evaluation-optimization --strict` 并修复所有问题。
- [ ] 6.3 完成至少七组通道消融和一轮参数 sweep，保存逐题结果与报告。
- [ ] 6.4 对高差异样本进行人工抽检，冻结 mapping overlap 阈值与 metric version。
- [x] 6.5 更新 PRD、交接文档和使用说明；若要改变生产默认策略，另开变更，不在本变更中直接切换。

### 2026-10-21—22 验收备注

- 修复后七组消融均已执行并保留逐题工件；六组成功，multi-query 因 982 个缺失 query embedding 造成 844 个系统失败。`6.3` 不能据此关闭。
- full-hybrid 参数网格共 24 个唯一组合（candidate limit 50/100 × overfetch 3/5 × RRF k 20/60 × rerank threshold 0/0.15/0.3），全部成功；保存 24120 个逐题结果，未隐式调用 provider。
- 40 例人工复核 JSON 与中文清单已准备，但 reviewed cases 仍为 0。`6.4` 保持待办，overlap 阈值尚未批准。
- 完整 smoke、最终定向回归、隔离提交快照、compile、OpenSpec strict 均通过，见 `evidence/` 下两个 20261021 摘要。未改变生产默认策略或将 fixture 延迟冒充生产测量。

参数网格最终 `report` 于 2026-10-22 通过，内部复核全部 24120 个逐题工件及 score/hash；`20261021` 命名保留运行发起日期，不覆写历史工件。
