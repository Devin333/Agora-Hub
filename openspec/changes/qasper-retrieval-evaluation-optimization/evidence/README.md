# QASPER 第 1 组：基线与合同证据

## 边界

本目录只提交小型身份、摘要和验证说明；不包含 QASPER 原始论文/标注、embedding vectors、provider 响应或凭据。

`qasper-dev-baseline.json` 冻结外部历史摘要及本次离线重建的内容身份。两者不是同一次执行：

- 历史结果：`F:\datasets\qasper\results\qasper_dev_optimized_full.json`。
- 原始数据：`F:\datasets\qasper\train-dev\qasper-dev-v0.3.json`。
- 原有缓存：`F:\datasets\qasper\cache\qasper_dev_text-embedding-v4_1024.npz`。
- 来源脚本：`F:\datasets\qasper\evaluate_qasper_dev.py` 和 `F:\datasets\qasper\evaluate_qasper_optimized.py`。冻结过程只读取/散列脚本，不执行它们。

## 不能倒填的历史身份

历史 JSON 只保存汇总分数，缺少逐题记录、历史代码/工作区指纹、冻结 mapping、版本化 metric 和成本/延迟明细。交接声明的 `f9b2530db83e2297cee7d2218fec9d75886d9c22` 保留为 `unverified_handoff_claim`，不是运行时观察证明。

本次确定性重建使用真实 `ResearchDocument` 和 `PaperDocumentChunker`，记录当前 commit、工作区内容指纹及 dirty patch 指纹。`corpus_sha256` 与 `chunk_sha256` 描述的是这次重建，不是对旧实验历史索引的追认。基线固定为 `diagnostic_only`，旧分数使用 `legacy_*` 指标身份，不能直接与新口径同比或用于生产发布门禁。

## 已生成快照

- `qasper-dev-baseline.json`：已对外部文件、真实重建的 281 篇论文与 17048 个 chunk 执行冻结，并通过 JSON 反序列化及摘要重算校验。快照 digest：`0a4611062b852b2cf463ff531503dca1a51a9eef29d4259648c37dbe642f8e91`。
- 观测重建与历史摘要的 mapping counts 不同：当前为 881/1005 个可评测问题、1766/1882 条文本 evidence 映射；旧摘要声明 888/1005、1787/1882。以 `explicit_diagnostic_mismatch` 保留两个来源各自的计数和旧分母，禁止混作一个结果。

## 分母与单位

- 计划问题：1005；本次当前代码重建可映射问题：881；当前重建不可映射问题：124。历史结果摘要声明的可评测分母仍为 888（不可把两者混用）。
- 论文：281；重建 chunk：17048。
- 当前重建 text evidence：1882 条，其中 1766 条映射到 paragraph chunk；float evidence：253 **条 evidence**；unanswerable：163 **条 annotation**。历史摘要对应的 text evidence 映射计数为 1787。
- 后两项不是互斥的问题分区，不能与问题数相加，也不能用于缩减 planned denominator。
- 此运行是 NumPy/in-memory `fixture`，即使缓存来自真实 embedding 服务，也不能证明 Qdrant 的生产延迟。

## 合同实现

- `backend/research/rag/evaluation/qasper_protocol.py`：不可变 baseline、共享 `ExperimentManifest` 的 QASPER 包装、mapping/case/failure/report DTO，以及严格版本与身份兼容校验。
- `backend/research/rag/evaluation/qasper_baseline.py`：显式路径的离线读取、流式 SHA-256、真实文档/chunk 重建、缓存形状/键/数值校验及摘要一致性校验。
- 复用现有 `protocol_common`、`protocol_execution`、`protocol_evidence_mapping` 与 `protocol_comparison`；不新增 runner、event store 或评分执行器。
- Strict/Equivalent 的 Hit、Coverage、MRR、nDCG 分开标识；非 ok 测量保留 null 和失败原因；报告必须覆盖全部计划单元并保留非可评测分母。
- Equivalent overlap 阈值仍是 `pending_human_review`，未冻结任何未经人工审核的阈值。分配 v1 合同版本不等于已完成第 2 组评分或第 6 组人工审核。

## 外部工件校验

数据/缓存/历史结果的已核验 SHA-256：

| 工件 | SHA-256 |
| --- | --- |
| dataset | `2ae7ee62a65b1c4225791c70de80c2aad4e8998cf1fd4f09a53103db4f21af93` |
| embedding cache | `57a2b191e3330a04d451f25d4d05f5abb51532a920c7224f9fbe23b6ba8cfff6` |
| legacy result | `e259baf46cf0cecfd759d3df2b184c74e6f69e00e7f77896d25877c929d21b6d` |

这些摘要用于验证当前外部文件未漂移，不将外部完整数据提交入库。第 1 组已完成：baseline snapshot 与合同 schema 已生成并通过离线验证。这是第 1 组完成时的历史记录；后续实现和验收以本文的继续开发记录及最新证据摘要为准。

## 继续开发记录（2026-10-21）

第 1 组的快照是历史身份，不因后续实现而覆写。本轮真实 `mapping freeze` / `mapping validate` 覆盖 1005 道问题，冻结 digest 为 `330fb4722d01da762584e3cf37aba3f0c469ab0eb1290bf158814231d35b6465`：844 道 valid mapping、101 道 needs_review、60 道 not_applicable。这个新分区与历史的 888/881 可评测计数口径不同，不直接比较成绩。

缓存审计工件位于仓库外 `F:/datasets/qasper/results/qasper-retrieval-eval-20261021/cache-audit.json`。当前 cache 对 valid mapping 的 multi-query 缺少 982 个精确文本 key，影响全部 844 道问题；已保存外部 `missing-multi-query-embeddings.jsonl`，其 SHA-256 为 `5c4d92d0c212ab5d1d6df773301d6c3fec1a124cd5dacaebca75d84fd34c96f2`。未调用外部 provider，也未合成或替代缺失向量。

先前 `qasper-seven-profiles-v2-debug` 与本轮 `ablation` 目录是诊断工件：六组有测量，第七组记录缓存失败。随后审查发现 CLI 的 RRF 类型、候选截断、空 rerank 阶段和 coverage 汇总等实现问题；修复前工件不作为最终验收或生产质量证据，不覆写这些历史记录。最终重跑应使用新的独立目录和最终代码指纹。

24 点参数 sweep 已完成；`6.3` 仍需补齐 multi-query 缓存并完成第七组成功实验；`6.4` 仍需人工复核并冻结阈值。自动化生成候选清单不等于人工审核，LLM 不得自行批准。

### 修复后验收进展

- `python -m scripts.dev smoke`：4947 passed、1 skipped、23 deselected；后续 AgentLoop 离线 smoke 与 source validation 均通过。长会话在部分新增回归测试落地前已开始，因此另外执行最终源码定向回归及隔离提交快照测试，不用旧 collection 代替新增测试。
- 最终 QASPER/边界回归：246 passed；隔离提交候选（其余源码恢复 HEAD，仅复制本次必要依赖）：467 passed；隔离 compile 与真实数据 mapping validate 均通过。
- QASPER 和 Harness caller inventory 对应 OpenSpec strict validation 均通过。
- 修复后七组消融位于外部 `final-ablation`：run `577ea4b5-086c-43e9-b0b1-03c5c4bd9cd0`；六组各 844 valid、0 system failure，第七组 0 valid、844 system failure（缺失精确 multi-query cache）。每组仍保留 1005 planned、101 needs_review、60 not_applicable。score 复核 7035 个逐题结果；report 完整生成。
- 外部 `human-review-packet.json` 包含 20 个高 Strict/Equivalent 差异样本、10 个对照和 10 个 needs_review mapping。SHA-256 `53bfb4e2a1b2ba55a9c63b0c0b4b14214bb0e05802bd98a2c9c2a901784bb410`，全部为 `pending_human_review`，尚无 reviewer 或阈值批准。

### 最终参数网格复核（2026-10-22）

24 个唯一 full-hybrid 参数组合已全部成功执行，保留 24120 个逐题结果（20256 valid、2424 needs_review、1440 not_applicable），0 system failure、0 external request。`report` 内部重新执行 score/hash 校验，完整复核并生成 `report.json` / `report.md`；小型指标、完整参数和工件 SHA-256 见 `qasper-dev-sweep-20261021.json`。目录和文件中的 `20261021` 是运行发起日期，最终报告复核于 2026-10-22 完成。

本轮固定参数的无重排 full-hybrid 为 StrictHit@5 0.6303、EquivalentHit@5 0.6967；24 点 fixture lexical reranker 网格的对应范围为 0.3448–0.3626 与 0.6090–0.6220。这是开发集、固定冻结分母上的诊断观察，不是生产 reranker 结论、显著性检验或发布决定；没有据此改变任何生产默认策略。

中文逐题清单 `human-review-checklist.md` 的 SHA-256 为 `ae4f1f88c0e9159f74460c39ee86ab05ae9837924a8a8d6a0dd4eb0b560bd024`。JSON 和 Markdown 原文均留在仓库外，reviewed cases 仍为 0。

代码与必要依赖已提交 `87aa6e15`。仅剩 `6.3` 的完整 multi-query 缓存/成功复跑，以及 `6.4` 的人工审核与 overlap 策略批准；变更尚不应归档为全部完成。
