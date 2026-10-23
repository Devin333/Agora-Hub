# QASPER 检索召回对照证据（2026-10-24）

## 结论与配置决策

同一 300 题、同一缓存和映射、同一最终上下文预算下，扩大候选池并启用字段索引后，候选集合的完整 gold 证据覆盖达到 215/224（96.0%），但最终上下文仅覆盖 110/224（49.1%）。扩大召回确实提高了候选覆盖，收益没有充分进入最终上下文。

当前轻量词法 reranker 的实际调用已确认，但它使字段方案的最终上下文等价覆盖从 110/224 降至 101/224，严格覆盖从 64/224 降至 43/224。因此本次不将实验配置提升为默认：默认仍为 `chunk_only`，`field_only` 和 `field_lightweight` 都必须显式选择。这里没有实现或运行 CrossEncoder。

本次只测量生成之前的检索和上下文，不构建回答模型或 judge。不能把这些指标与原回答运行的上下文覆盖 119/224（53.1%）直接横比，也不能声称原端到端正确率 50.9% 已改善。两种运行的 Harness replan 和上下文组装路径可能不同。

## 固定对照结果

四个 profile 均复用原始 300 题及其顺序。其中 224 题具有可计分的有效 gold 映射，31 题为 `needs_review`，45 题为 `not_applicable`。所有 coverage 分母固定为 224；后两类不伪装成系统失败，也不从计划样本中删除。

以下覆盖均为完整证据覆盖率。本样本每个可计分项的有效证据组分母为 1，因此 `macro_case_fraction`、`micro_evidence_groups` 和 `complete_case_rate` 恰好相同；实现与工件仍分别记录三种口径。

| profile | overfetch | 候选 equivalent | 候选 strict | 最终上下文 equivalent | 最终上下文 strict |
|---|---:|---:|---:|---:|---:|
| `baseline_chunk` | 3 | 160/224 = 71.4% | 135/224 = 60.3% | 106/224 = 47.3% | 63/224 = 28.1% |
| `expanded_chunk` | 10 | 208/224 = 92.9% | 203/224 = 90.6% | 86/224 = 38.4% | 49/224 = 21.9% |
| `expanded_field_without_rerank` | 10 | 215/224 = 96.0% | 215/224 = 96.0% | 110/224 = 49.1% | 64/224 = 28.6% |
| `expanded_field_rerank` | 10 | 215/224 = 96.0% | 215/224 = 96.0% | 101/224 = 45.1% | 43/224 = 19.2% |

- 每组均有 300 条逐题记录，系统失败均为 0；总计 1,200 条。
- 最终预算固定为 8 个上下文条目、2,048 tokens；实测最多 8 条，context-size gate 记录的最大 token 数为 1,411。
- reranker 组在 297 题实际调用，总调用次数 985；其余三组均为 0。该组同时注入基础和字段 `LightweightLexicalReranker`，真实调用继续服从现有 intent policy gates。
- 四组的 `succeeded / insufficient_evidence` 分别为 260/40、253/47、269/31、269/31。`insufficient_evidence` 是受控业务结果，不是系统异常。
- 候选已完整覆盖、最终上下文却未完整覆盖的题数分别为 62、123、106、115。上下文扩展也可能引入原始候选集合之外的 gold，因此不能直接用两个聚合分子的差代替逐题流失数。

字段无 reranker 方案相对本次 baseline 净增加 4 道 equivalent、1 道 strict 命中；这不是统计显著性结论。后续应按已有逐题 provenance 和 transcript 进一步拆解排序、证据筛选、父上下文扩展及最终选择阶段。当前证据尚不足以把损失归因到其中某个具体函数或权重。

## 实现边界

- answer evaluation 新增不可变 profile、可选字段索引/基础 reranker/字段 reranker ports，以及完整策略、组件类名、固定预算和 profile fingerprint。
- CLI 支持 `--retrieval-profile chunk_only|field_only|field_lightweight` 与正整数 `--retrieval-overfetch`。例如显式 `--retrieval-profile field_only --retrieval-overfetch 10` 对应字段无 reranker 实验方向；本次没有因此启动 live answer run。
- 字段缓存不完整或身份不匹配在首题之前失败；选定字段方案后不会静默退回 chunk-only。
- replay 走真实 `PaperRAGSession` 和现有 deterministic gates，关闭 generation；只读 query vector resolver 不包含 live fallback。
- 每题记录候选 query/channel/rank/score、实际上下文 ID、reranker 调用、mapping disposition、状态、失败和 durable transcript。
- application 层只接收 ports，组件由 composition 组装；未改变生产 Research 默认配置、Qdrant、业务 memory、active skills、原 gold 映射或原回答评分协议。
- 内存缓存扫描是 fixture 对照，不能当作生产 Qdrant 的质量或时延基准。

## 缓存预检与独立补全

字段预检发现 17,048 个 chunk、32,302 个不同的非空字段文本、45,068 条基础向量记录，缺失字段向量为 0。

首次完整 replay 在 `F:/datasets/qasper/results/qasper-answer-retrieval-replay-20261024035952` 按预期被阻断：扩大候选集改变了 Harness 的 controlled replan，产生 5 个未缓存的真实查询。阻断工件不发布任何 coverage，`cases.jsonl` 为空。诊断增强后的复查工件保留在 `F:/github/NewsRoom/.tmp/qasper-retrieval-replay-debug-202610240407`，记录缺失 query text/digest、case、profile、channel 和查询阶段。

在 replay 之外，复制原 supplementary query cache，并使用同一个真实 `dashscope/text-embedding-v4`、1,024 维 provider 补齐这 5 条向量：299 条增加为 304 条，真实 embedding 调用 5 次，回答和 judge 调用 0 次。没有生成假向量或修改原缓存。

- 原 query cache：`F:/datasets/qasper/cache/qasper_dev_query_vectors_text-embedding-v4_1024.npz`
- 新 query cache：`F:/datasets/qasper/cache/qasper_dev_query_vectors_text-embedding-v4_1024_retrieval_replay_20261024.npz`
- 准备记录：`F:/datasets/qasper/cache/qasper_dev_query_vectors_text-embedding-v4_1024_retrieval_replay_20261024.preparation.json`
- 新缓存 SHA-256：`7949d8c9cd10f396d36b39f37c17f314d9f32665c40432e9f7463dc616d15f79`
- 新缓存内容 fingerprint：`c644cb27ff83afb222cf0d59f6e0fbecdb6e532c43ef006557707801ae0a06ce`

最终 replay 的四组共享这份冻结缓存，`base_hits=281`、`supplementary_hits=2684`、`live_calls=0`、`missing_keys=0`。

## 最终运行身份与复现

最终工件目录：`F:/datasets/qasper/results/qasper-answer-retrieval-replay-20261024041637`。协议为 `qasper_retrieval_only_replay_v1`，状态 `completed`，耗时 131,887.6 ms。运行时为提交前工作区，基准 Git revision 为 `dd14ae050472db3a5421ffbd76fb28d5c2e07f73`、`dirty=true`；完整代码身份保存在 manifest，不能误写成该基准提交的干净版本。

| 身份/工件 | SHA-256 或 fingerprint |
|---|---|
| `report.json` | `136aae0c742a9d893b10302c928e1bfc04570434fbc4904cbd7258ea80482a23` |
| `manifest.json` | `4f08f04e60d8d19268f038cdd1b1fe127940f1d964562b259a278ef29af5cf35` |
| `cases.jsonl` | `9793b6c3134289aeda64b969b2719aaeb639faeee6166e1144527e261df2f4ae` |
| workspace content | `b02256d53816a581251d535bb2edb023d5a694e560a6cc50923459b28451b6c6` |
| code fingerprint | `f6f9999afb0c1dcd4bfc8e5365f6984a486756048c3477ef75bc3c87fc89b7cd` |
| dataset | `2ae7ee62a65b1c4225791c70de80c2aad4e8998cf1fd4f09a53103db4f21af93` |
| base embedding cache | `57a2b191e3330a04d451f25d4d05f5abb51532a920c7224f9fbe23b6ba8cfff6` |
| mapping | `330fb4722d01da762584e3cf37aba3f0c469ab0eb1290bf158814231d35b6465` |
| corpus | `39a05bb2a27c195605e431e045593485528678d1d5cc15fd3d6406a483ae76c0` |
| chunk index | `6afa46b2e96aaa8ee955c57319ea03e2a381036419660877938c54314af5a09b` |
| sample case IDs | `ed2873ce097e7705aa974a823e6c32f8f96b4f3dbc34ee32f762b3593092de50` |

复现时使用一个尚不存在的新输出目录，保留已发布工件：

```powershell
python -m scripts.dev rag-eval qasper `
  --dataset-path F:/datasets/qasper/train-dev/qasper-dev-v0.3.json `
  --cache-path F:/datasets/qasper/cache/qasper_dev_text-embedding-v4_1024.npz `
  answer replay `
  --source-run-dir F:/github/NewsRoom/.tmp/qasper-answer-full-20261023221544 `
  --query-vector-path F:/datasets/qasper/cache/qasper_dev_query_vectors_text-embedding-v4_1024_retrieval_replay_20261024.npz `
  --output-dir F:/datasets/qasper/results/qasper-answer-retrieval-replay-verification
```

首个成功对照目录为 `F:/datasets/qasper/results/qasper-answer-retrieval-replay-20261024041142`。最终快照再次运行后，1,200 条记录的候选 ID/顺序/分数/provenance、上下文 ID、coverage、profile fingerprint、reranker 调用及业务状态逐题一致；时间、随机 transcript 标识及代码快照身份不参与内容确定性比较。

原始运行和原 query cache 的 SHA-256 在最终核验后仍保持不变：

| 原工件 | SHA-256 |
|---|---|
| 原 `report.json` | `831c8de9b542ee3be556dcc224bdc87a39c02a36431396c561d7148fac6b5a64` |
| 原 `answers.jsonl` | `893b62dad3cf2dbb80cfdf1c5a68c0da17a97ecc3c8aabe7b39ba67fa674ac40` |
| 原 `sample.json` | `6d169d937bbba321eac4f0484dcebd827de69b2c3afd54c889b857b6bb6ea429` |
| 原 query cache | `79c5180d8defff4ec2ee747a7d3325ed8a26db2d53a16411683d54042dbe398c` |

## 检查与未完成的后续工作

- `python -m scripts.dev smoke`：退出 0；5,325 passed、1 skipped、23 deselected；offline AgentLoop smoke 的 network calls 为 0，source validation 无错误或警告。
- 最终全部 QASPER 测试：297 passed；变更面与全部 architecture 的组合检查：322 passed。
- `python -m scripts.dev compile`、`git diff --check`、新旧两项 OpenSpec strict validation 均通过。
- 真实 overfetch 集成测试确认扩大实际候选数量，而最终上下文仍不超过 8 条；测试覆盖 profile 身份稳定性、实际 reranker 调用、空/受限 intent allowlist、身份不匹配、字段缺失、运行时查询缺失及无 live fallback。
- 50 题人工校准包与空白 `labels.csv` 已准备，仍等待人工标签；旧变更的 4.8 保持未完成，未声称 judge 已校准。
- 回答约束、是否题提示词、失败记录原始模型输出和逐题进度属于后续生成端/可观测性变更；本次未修改这些行为，也未重新运行 live answer 或 judge。
