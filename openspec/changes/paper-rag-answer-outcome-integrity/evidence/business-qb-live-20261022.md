# 业务题库首次真实模型评测（2026-10-22）

- 运行目录（未入库）：`.newsroom/eval/business_question_bank_v1_live_20261022T123622Z`
- 命令：`python -m backend.research.rag.cli.run_business_answer_eval`
- 生成模型：`gpt-5.6-sol`（Research 路由选择键解析）；本次未调用 judge
- 语料：`data/eval/golden_corpus_snapshot.json`（35 chunks / 20 papers）；检索为内存 fixture 哈希索引，**不代表生产检索质量**
- 覆盖：题库 90 行，评分 42（单篇 30、拒答 12），48 道草稿题跳过（needs_annotation）

## 结果

| 指标 | 值 | 分母 |
|---|---|---|
| answered / abstained / insufficient_evidence / failed | 16 / 13 / 12 / 1 | 42 |
| 系统失败率 | 2.4% | 42 |
| 拒答准确率（应拒答题） | 100% | 12 |
| 过度拒答率（应回答题） | 44.8% | 29 |
| 回答成功率 | 59.5% | 42 |
| 引用 gold 覆盖 | 0.81 | 16 |
| 上下文 gold 覆盖 | 0.79 | 29 |
| 端到端延迟 P50 / P95 | 7.6s / 40.5s | 42 |
| 每次模型调用 tokens（输入 / 输出） | 4072 / 48 | 29 |

## 单篇题 30 道的去向

- 16 answered：13 成功；3 道 `citation_missing_source`（single_001/002/008 引用了非 gold chunk，是否等价待人工确认）
- 7 模型拒答（上下文里有 gold）：5 道 gold chunk 渲染后超过 1000 字被 `_compact_context_text` 截断（single_019/023/026/027/028），2 道未截断（single_004/009）。截断是否删掉了答案所在句子**尚未逐题核实**
- 6 insufficient_evidence（single_011/012/014/021/029/030）：检索证据类型与题目要求不符，上下文覆盖为 0
- 1 failed（single_007）：`model_malformed_output`，模型在拒答前缀之外附加了其他文本

## 结论边界

数字来自 fixture 检索和 42 题小样本，只用于定位问题方向，不作为质量结论或发布依据。
