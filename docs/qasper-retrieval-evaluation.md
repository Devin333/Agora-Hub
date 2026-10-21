# QASPER 检索评测（离线）

该适配器用于冻结 evidence mapping、运行检索通道消融/参数 sweep，并生成可追溯的 Strict/Equivalent 评测结果。它不会更新生产索引、生产默认检索策略或业务状态；本地 fixture/in-memory 结果也不能代表 Qdrant 生产延迟。

## 安装

离线评测使用 NumPy；安装项目对应的可选依赖：

```powershell
python -m pip install -e ".[qasper-eval]"
```

普通帮助和参数解析不需要配置任何 provider 凭据。测试需另安装项目开发依赖。

## 外部数据

不要把 QASPER 原始数据、embedding cache 或大体积逐题实验结果复制进仓库。将授权获得的 QASPER 数据集 JSON 与已有 embedding cache 保存在仓库外，并通过命令行显式传入绝对路径：

- `--dataset-path`：QASPER JSON 文件；
- `--cache-path`：本地 `.npz` embedding cache，须含 `keys` 与二维 `vectors` 数组。工具只读已有 cache，不调用 embedding provider。

输入缺失、cache key/向量不匹配或必要 embedding 不存在时，操作会失败而不会联网补算。

## 常用命令

以下命令通过既有 `rag-eval` CLI 注册；输出目录须显式指定。路径示例需替换成当前机器的实际外部数据位置。

```powershell
python -m scripts.dev rag-eval qasper --dataset-path F:\datasets\qasper\qasper-dev-v0.3.json --cache-path F:\datasets\qasper\embeddings.npz mapping freeze --output-dir F:\datasets\qasper\frozen
python -m scripts.dev rag-eval qasper --dataset-path F:\datasets\qasper\qasper-dev-v0.3.json mapping validate --mapping-dir F:\datasets\qasper\frozen
python -m scripts.dev rag-eval qasper --dataset-path F:\datasets\qasper\qasper-dev-v0.3.json --cache-path F:\datasets\qasper\embeddings.npz ablation run --output-dir F:\datasets\qasper\runs --mapping-dir F:\datasets\qasper\frozen --profile full_hybrid --candidate-limit 100 --overfetch 3 --rrf-k 60 --rerank false --threshold 0 --weights "{}"
python -m scripts.dev rag-eval qasper --dataset-path F:\datasets\qasper\qasper-dev-v0.3.json --cache-path F:\datasets\qasper\embeddings.npz sweep --output-dir F:\datasets\qasper\sweeps --mapping-dir F:\datasets\qasper\frozen --profiles semantic_only bm25_only field_only semantic_bm25 semantic_field full_hybrid full_hybrid_multi_query --candidate-limit-grid "[100]" --overfetch-grid "[3]" --rrf-k-grid "[60]" --rerank-grid "[false]" --threshold-grid "[0]" --weights-grid "[{}]"
python -m scripts.dev rag-eval qasper score --run-dir F:\datasets\qasper\runs
python -m scripts.dev rag-eval qasper report --run-dir F:\datasets\qasper\runs
```

sweep 按显式维度生成笛卡尔积；上面的示例是每个通道 profile 一个参数点，不会默认启动完整大网格。扩展 `candidate_limit`、`overfetch`、`rrf_k` 和 threshold 时应评估总组合数，保存全部组合而非只保留最好结果。`mapping validate` 需要相同的 `--dataset-path`，用于校验冻结工件与实际输入一致，不需要 embedding cache。

`--output-dir` 本身就是后续 score/report 的 `--run-dir`，不自动追加 `<run-id>` 子目录。输出工件不可变：不同运行使用不同目录；冲突不得覆盖历史结果。

## 工件与解释

每次运行绑定 dataset、corpus/index、冻结 mapping、代码身份、embedding cache、profile、参数和 execution profile。保留逐题 mapping 状态、完整计划分母、可评测分母、通道 provenance 和各指标分子/分母。`needs_review` 映射问题、真实检索未命中和系统失败分别统计。Strict 与 Equivalent 具有独立版本，后者不能替代前者。

QASPER Dev 是开发集，调参结果不能当作 held-out 泛化证据，也不能直接据此修改生产默认值。原始运行工件保存在仓库外；仓库只提交经过审查的小型身份和诊断摘要。

## 已知缓存边界（2026-10-21）

当前外部缓存的 SHA-256 为 `57a2b191e3330a04d451f25d4d05f5abb51532a920c7224f9fbe23b6ba8cfff6`。针对冻结 mapping `330fb4722d01da762584e3cf37aba3f0c469ab0eb1290bf158814231d35b6465` 的 844 道可评测问题，multi-query 共需 1779 个唯一 query key，其中 982 个缺失，影响全部 844 道问题。缺失 key 清单位于仓库外 `F:/datasets/qasper/results/qasper-retrieval-eval-20261021/missing-multi-query-embeddings.jsonl`。

因此，现有缓存不能证明 multi-query 运行成功。不得跳过变体、以原 query 向量代替、合成向量或隐式调用付费 provider 来制造完成状态。需要提供包含这些精确文本的同模型缓存，再在新输出目录重新运行。mapping overlap 阈值仍待人工抽检批准；不能由 LLM 自行把 `pending_human_review` 改为通过。

## 评分与报告口径

最终阶段通过必填 `final_stage` 明确记录；空 rerank 输出不会退回 fused 候选计分。无 rerank 的结果也必须遵守 `candidate_limit`。总体指标按每题指标值作宏平均，逐题证据分母保留在 `cases.json`，不把 evidence group 数量相加后冒充 case 分母。报告同时保留 Strict/Equivalent 的 Hit、Coverage、nDCG `@5/@10` 和 MRR。

`report.json` 保存完整切片和失败归因，`report.md` 与各 variant 的 `*-report.md` 提供可读入口。切片覆盖 question type、paper、section/field、evidence count、mapping status 和 first-hit rank；延迟/成本明确区分 index、request、judge 及各执行阶段。真实 QASPER 输入未提供 question type 时，只为诊断使用版本化 `qasper-question-type-rules/v1`，标记 `derived_not_gold`，不写回 gold 或冻结 mapping。

`score` / `report` 的 `valid=true` 表示保存工件、哈希和评分复核通过，不代表每个 experiment 执行成功，更不是生产 quality gate。判断执行情况必须检查每组 `denominators.system_failure`；缺失缓存组会保留所有失败题，不伪装为零召回或删除分母。

## 本轮实测与剩余动作

修复后七组消融位于外部 `F:/datasets/qasper/results/qasper-retrieval-eval-20261021/final-ablation`；六组成功，multi-query 因上述缓存缺口明确失败。`final-sweep` 保留 24 个 full-hybrid 参数组合和 24120 个逐题结果，全部执行成功且无外部请求。仓库中的小型身份/指标摘要位于 `openspec/changes/qasper-retrieval-evaluation-optimization/evidence/qasper-dev-ablation-20261021.json` 与 `qasper-dev-sweep-20261021.json`。这不是生产默认参数变更或 held-out 结论。

外部 `human-review-packet.json` 与 `human-review-checklist.md` 提供 40 例审阅材料。人类需核对 required groups、exact child 完整路径和 Equivalent 证据充分性，并以新文件记录审阅者、时间、依据和阈值决定；原包不可覆盖。当前全部为 `pending_human_review`，不能用 LLM 自动解释代替审核。
