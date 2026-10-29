# Jev 与现有 RAG 排序器对照实验

实验日期：2026-09-20。实验变更：`jev-rerank-comparison`。

## 已测结果

本轮抽中的 24 个问题来自 14 篇论文。每题的标准证据均进入 12 个候选片段，候选覆盖率为 100%。

| 模型 | 完成数 | Hit@1 | Hit@3 | Hit@5 | MRR | nDCG@5 | 查询 p50 | 查询 p95 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 现有词法排序 | 24/24 | 79.17%（19/24） | 87.50% | 95.83% | 0.8559 | 0.8783 | 1.31 ms | 1.42 ms |
| BGE v2-m3，CPU | 24/24 | 91.67%（22/24） | 100% | 100% | 0.9514 | 0.9638 | 4,372.55 ms | 4,681.71 ms |
| Jev 1.13.0 | 未测 | — | — | — | — | — | — | — |

BGE 在这批固定样本上的首位命中比词法排序多 3 题（12.50 个百分点），MRR 增加 0.0955。BGE 模型初始化约 3.54 秒，首次预热约 5.12 秒，均不计入表中的查询时延。这些是单轮小样本观测，没有统计显著性或生产收益结论。

执行机器为 Intel Core i7-14700KF、约 32 GB 内存，Python 3.12.7、PyTorch 2.13.0+cpu，PyTorch 使用 8 个线程。BGE 使用本地缓存 revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`，推理 batch size 为 8。

Jev 尚未调用：本地未配置 `TYPESAFE_API_KEY`。因此目前不能判断 Jev 相对 BGE 的质量、时延或费用。

本轮 manifest fingerprint 为 `sha256:40e60d1a49a491f5c9f29ca2ac21019c8ebc21c232cfe2627103fa357589574f`，原始输入和逐题报告保存在 `outputs/jev-rerank-pilot-20260920/`。

## 实验问题

在同一批问题与相同候选片段上，Jev 的相关性评分能否改善已有排序，同时满足可接受的调用时延与成本？本实验只考察排序，不生成答案、不判断发布资格，也不改变生产运行路径。

## 固定方法

- 数据来自仓库已审核的 `data/eval/golden_set.json`，包含 79 个问题，其中 67 个可回答、12 个应拒答。
- 本机完整论文解析库缺失，使用经过内容校验的 `golden_corpus_snapshot.json`：20 篇论文、35 个真实片段。这是用于测试的小型证据集合，不能代表完整知识库。
- 使用固定 seed 选择 24 个可回答问题。拒答题不适合仅靠排序评判，本轮排除并记录数量。
- 对每个问题先对全部 35 个片段执行现有词法排序，取前 12 个候选。候选生成不读取 gold 标签，不把漏召回的 gold 强行放回候选。
- 比较现有词法排序、`BAAI/bge-reranker-v2-m3`、固定版本 `jev-1.13.0`。模型输入只包含问题、片段 ID 和原文；gold 仅用于离线计分。
- Jev 使用四级相关性 Score，默认每批至多 8 个片段，逐片段明确提问。请求有超时和次数上限，不自动重试、不用其他模型结果填补失败。
- 每个模型单独记录初始化与一次预热，预热不计入查询延迟；Jev 预热消耗仍计入总 token 与费用。

## 评价与解释

报告 Hit@1/3/5、MRR、nDCG@5、候选覆盖率、完成率、逐题排名和 p50/p95 查询延迟。失败及未测状态单独保留；模型之间只对相同成功样本计算配对质量差异，并同时报告全部样本的覆盖情况。

这台机器的 PyTorch 为 CPU 版本。BGE 延迟反映本机 CPU 推理；Jev 延迟包含网络往返，不能据此推断 BGE 在 GPU 上的性能。BGE 沿用现有 512-token 上限，因此接收相同原文不代表两个模型实际可见 token 完全相同。gold 标签可能没有穷尽所有同样有效的证据，因此标签分数不是完整的语义正确率。

候选池中有 5 个片段仅正文就超过 512 token，最长约 3,499 token。部分问题使用 “the text” 等依赖原论文语境的表述，在跨论文候选池中存在歧义；这也是本轮不能代替论文内检索评测的原因。下一轮应取得完整论文片段，保持论文作用域，并扩充人工核对的相关性标签。

Jev 官方当前标价为每百万输入 token 0.042 美元，输出 token 不收费。报告中的美元值属于按实际观察到的 token 用量乘以指定单价得到的估算，不是账单；本地算力、电费与托管成本未测，不能写成零成本。[模型与价格](https://docs.typesafe.ai/models)

## 接入边界

模型只提供排序分数；Harness 的流程控制、确定性 gate、memory 写入和 publication 权限保持既有合同。API key 仅从环境或显式指定的本地 env 文件读取，不写入报告或版本库。

官方协议参考：[API](https://docs.typesafe.ai/api)、[置信度](https://docs.typesafe.ai/confidence)。

## 复跑命令

以下命令在实验 worktree `F:/github/NewsRoom/.tmp/jev-rerank-experiment` 中运行，使用项目现有虚拟环境。新一次实验使用新的输出目录，脚本拒绝覆盖已有结果。

```powershell
$experimentPython = 'F:/github/NewsRoom/.venv/Scripts/python.exe'
$experimentOutput = 'outputs/jev-rerank-reproduction'
& $experimentPython -m scripts.jev_rerank_dataset --output "$experimentOutput/manifest.json"
& $experimentPython -m scripts.jev_rerank_experiment run --arm lexical --dataset "$experimentOutput/manifest.json" --output "$experimentOutput/lexical.json"

$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:OMP_NUM_THREADS = '8'
$env:MKL_NUM_THREADS = '8'
& $experimentPython -m scripts.jev_rerank_experiment run --arm bge --model 'C:/Users/Administrator/.cache/huggingface/hub/models--BAAI--bge-reranker-v2-m3/snapshots/953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e' --batch-size 8 --dataset "$experimentOutput/manifest.json" --output "$experimentOutput/bge.json"

& $experimentPython -m scripts.jev_rerank_experiment run --arm jev --dataset "$experimentOutput/manifest.json" --output "$experimentOutput/jev.json" --env-file F:/github/NewsRoom/.env --input-price-per-million 0.042
& $experimentPython -m scripts.jev_rerank_experiment summarize --reports "$experimentOutput/lexical.json" "$experimentOutput/bge.json" "$experimentOutput/jev.json" --output "$experimentOutput/summary.json"
```

Jev 使用 `TYPESAFE_API_KEY`，配置在上面显式指定的本地 `.env` 中。补跑本轮 Jev 时，可直接复用已冻结的 `outputs/jev-rerank-pilot-20260920/manifest.json`，将输出文件另命名为 `jev-live.json`，再与已有 `lexical.json`、`bge.json` 生成新的 `summary-live.json`。默认参数下完整 Jev 运行最多需要 50 次请求（含预热），每次超时 30 秒，无自动重试。

## 验证命令

```powershell
& $experimentPython -m pytest tests/scripts/test_jev_rerank_dataset.py tests/scripts/test_jev_rerank_experiment.py -q
& $experimentPython -m scripts.dev smoke
openspec validate jev-rerank-comparison --strict
```
