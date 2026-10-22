# 证据类型 gate 修复前后对比（业务题库，2026-10-22）

- 修复前：`.newsroom/eval/business_question_bank_v1_live_20261022T142308Z`（已包含上下文预算修复）
- 修复后：`.newsroom/eval/business_question_bank_v1_live_20261022T202201Z`
- 两次运行都使用 `gpt-5.6-sol`、golden corpus snapshot 和内存 fixture 检索，共 42 题（单篇 30 题、拒答 12 题）。结果目录未入库。

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 结果分布 | answered 22 / abstained 8 / insufficient_evidence 12 / halted 0 | answered 31 / abstained 10 / insufficient_evidence 0 / halted 1 |
| 过度拒答率 | 26.7%（8/30） | 3.3%（1/30） |
| 拒答准确率 | 100%（12/12） | 81.8%（9/11，另有 1 题 halted） |
| 回答成功率 | 73.8% | 81.0% |
| 系统失败率 | 0% | 2.4%（abstain_002） |
| 上下文 gold 覆盖 | 0.80 | 1.00 |
| 引用 gold 覆盖 | 0.86 | 0.86 |

## 6 道被类型 gate 拦下的题

全部由 `insufficient_evidence` 变为答对：

| 题 | 路径 | 模型回答 |
|---|---|---|
| single_011 | B | 5-shot 实验跑了 5 次 |
| single_012 | B | 标准差 0.5 |
| single_014 | B | 主文引用的是第一次运行的结果 |
| single_021 | B | 100 名参与者 |
| single_029 | A | 失败原因是代码执行错误 |
| single_030 | A | Scenario 3 的标题是 "Multi-User Problem Solving" |

A 表示多类型解析后直接满足覆盖要求，B 表示 advisory 策略下交给模型判断。

## 退步与波动

- **abstain_002**：属于 B 路径，由本次改动引起。改动后该题进入模型，网关对"癌症临床试验"问题返回内容拒答（"service does not support biological or life-science research"）。这段文本没有引用，被 claim 覆盖 gate 判为 `halted`。
- **abstain_009 / abstain_011**：两次运行的上下文完全相同，也都没有走 B 路径。区别只在于，模型这次用 "No. The passages … do not …" 作答，没有使用拒答前缀。回答内容是正确的，属于模型输出不稳定，以及"是否题"与拒答协议之间存在歧义。
- **single_007**：上下文相同，这次给出了一个非 gold 的引用作答，属于模型输出波动。

## 结论边界

样本只有 42 题，检索是 fixture，且只跑了一次，模型输出的波动无法排除。拒答准确率的分母只有 11，一道题的变化就会带来约 9 个百分点的波动。
