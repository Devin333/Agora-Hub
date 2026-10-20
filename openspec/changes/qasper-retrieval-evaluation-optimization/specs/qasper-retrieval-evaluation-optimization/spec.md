# qasper-retrieval-evaluation-optimization Specification

## Purpose

为 Paper RAG 建立可复现、可解释且不通过放宽分母虚增成绩的 QASPER 证据检索优化评测能力。

## ADDED Requirements

### Requirement: Frozen evidence mapping

评测 MUST 使用在运行前冻结的 evidence mapping，且 mapping MUST 绑定 dataset identity、corpus snapshot、parser/chunker/index identity 和 mapping version。mapping MUST 支持 gold evidence 到 exact chunk、child/parent closure、相邻 chunk 和 normalized text span 的确定性关系。

#### Scenario: Exact child match
- **WHEN** 返回候选的 `chunk_id` 与冻结 gold anchor 的 child chunk ID 完全一致
- **THEN** 该候选 MUST 被 Strict 和 Equivalent 评测同时视为相关

#### Scenario: Parent-child equivalent match
- **WHEN** 返回 child chunk 属于 gold parent，或返回 parent 覆盖 gold child
- **THEN** Equivalent 评测 MUST 视为相关
- **AND** Strict 评测 MUST 只有在 ID 完全一致时才视为相关

#### Scenario: Ambiguous mapping
- **WHEN** gold anchor 无法唯一映射到当前 index chunk
- **THEN** mapping MUST 保留 `ambiguous` 或 `missing` 状态
- **AND** 评测 MUST 不得根据本次候选结果动态扩大 gold

### Requirement: Dual hit metrics

评测 MUST 同时输出有独立版本的 `StrictHit@K` 和 `EquivalentHit@K`。Strict 指标只接受 exact evidence/chunk ID；Equivalent 指标接受冻结 mapping 定义的内容等价关系。每个指标 MUST 保存 numerator、denominator、metric version 和 mapping version。

#### Scenario: Strict hit
- **WHEN** Top-K 中出现 exact gold evidence ID
- **THEN** `StrictHit@K` MUST 对该 case 记为命中

#### Scenario: Content-equivalent hit
- **WHEN** Top-K 中没有 exact gold ID，但候选通过冻结的 parent-child 或 normalized span mapping
- **THEN** `EquivalentHit@K` MUST 对该 case 记为命中
- **AND** `StrictHit@K` MUST 保持未命中

#### Scenario: No evidence in top K
- **WHEN** Top-K 中没有任何符合 mapping 的候选
- **THEN** 两个 hit 指标 MUST 对该 case 记为未命中
- **AND** failure record MUST 保留 query、排名和候选 provenance

### Requirement: Coverage and ranking semantics

Evidence coverage、MRR 和 nDCG MUST 使用冻结的 evidence group、原始候选排名和声明的截止位置。重复 chunk、重叠 span 或同一 parent closure MUST NOT 重复增加 coverage。命中一条证据 MUST NOT 被解释为完整证据充分。

#### Scenario: Multiple required evidence groups
- **WHEN** 一个问题包含多个共同必要 evidence groups
- **THEN** coverage MUST 分别计算每个 group 的覆盖并按声明口径聚合
- **AND** 一个 group 的命中 MUST NOT 抵消另一个 group 的缺失

#### Scenario: First relevant rank
- **WHEN** 第一个相关候选出现在原始排名第 r 位
- **THEN** MRR MUST 使用 `1/r`
- **AND** 去重 MUST NOT 压缩原始排名以改善 MRR

### Requirement: Retrieval ablation profiles

系统 MUST 支持显式、可追溯的通道 profile，至少包括 `semantic_only`、`bm25_only`、`field_only`、`semantic_bm25`、`semantic_field`、`full_hybrid` 和 `full_hybrid_multi_query`。每次运行 MUST 保存 profile fingerprint、各通道候选、去重结果和独立 gold evidence 贡献。

#### Scenario: Channel contribution
- **WHEN** 一个 profile 完成检索
- **THEN** 结果 MUST 区分各通道首次命中、候选数量、最终排名和新增 gold evidence

#### Scenario: Disabled channel
- **WHEN** profile 明确关闭某个通道
- **THEN** 该通道 MUST 不被隐式调用
- **AND** manifest MUST 记录关闭状态

### Requirement: Parameter sweep reproducibility

系统 MUST 支持 candidate limit、overfetch multiplier、RRF k、rerank threshold 和权重的显式实验矩阵。每个组合 MUST 有唯一 experiment ID，并绑定代码、数据、索引、embedding、profile 和输出工件身份。

#### Scenario: Same manifest replay
- **WHEN** 使用相同 manifest、相同数据和相同索引重复运行
- **THEN** 评测结果 MUST 可复现到声明的数值精度
- **AND** 运行 MUST 不修改 production index 或业务状态

#### Scenario: Rerank threshold comparison
- **WHEN** 分别使用 threshold 0、0.15 和 0.3
- **THEN** 报告 MUST 显示每个阈值的候选保留数、命中数和覆盖率
- **AND** 不得只保留得分最高的一次运行

### Requirement: Slice and failure diagnostics

报告 MUST 支持按 question type、paper、section/field、gold evidence count、mapping status 和 first-hit rank 切片，并区分 `needs_review`、`system_failure`、`not_applicable` 与有效测量。

#### Scenario: Unmappable question
- **WHEN** case 没有可验证的 text evidence mapping
- **THEN** case MUST 保留在 planned denominator 和 status report 中
- **AND** 不得静默删除或改记为检索负例

#### Scenario: Failure attribution
- **WHEN** case 在 Top-K 未命中
- **THEN** report MUST 尽可能区分 candidate recall miss、fusion/rerank miss、mapping failure 和 source/index failure

### Requirement: Execution profile separation

评测 MUST 区分 `fixture`/in-memory 与 `production`/真实检索链路执行身份。fixture 结果不得被描述为生产延迟或生产质量；生产 profile 必须记录真实 index、模型和请求成本/延迟身份。

#### Scenario: Fixture run
- **WHEN** 使用 NumPy in-memory adapter 运行 QASPER
- **THEN** manifest MUST 标记 `execution_profile=fixture`
- **AND** report MUST 明确该结果不代表 Qdrant 生产延迟

#### Scenario: Production run
- **WHEN** 使用真实 Paper RAG/Qdrant 链路运行
- **THEN** 结果 MUST 保存 index/config/model identity、P50/P95 latency 和实际调用状态
- **AND** 不得向生产业务状态写入评测数据
