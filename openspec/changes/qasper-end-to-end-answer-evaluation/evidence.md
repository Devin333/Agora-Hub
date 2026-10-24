# QASPER dev 端到端评测证据摘要

## 运行身份

- 协议：`qasper_end_to_end_answer_v1`，提交 `c9f90d37`
- 数据集：QASPER dev v0.3，sha256 `2ae7ee62…af93`
- 抽样：种子 `qasper-answer-v1`，extractive / abstractive / boolean / none 各 75 题，共 300 题
- 回答模型 gpt-5.6-sol，judge gpt-6.1-sol（`qasper_answer_judge_v1`）
- 检索：缓存向量内存扫描（text-embedding-v4，1024 维），仅 chunk store，无 reranker，无字段索引；生成最多 2 次尝试
- 不具备生产资格：检索没有走生产 Qdrant 索引

## 总体指标

| 指标 | 值 |
|---|---|
| 端到端正确 | 148/291 = 0.509 |
| judge 判已回答的题正确 | 100/179 = 0.559 |
| 引用支持率 | 0.788 |
| Answer-F1（官方口径，等量分层） | 0.347 |
| 无解题正确拒答 | 22/44 = 0.500 |
| 可答题过度拒答 | 54/203 = 0.266 |
| 系统失败 | 10/300（违反拒答协议 9，网关不可用 1） |
| judge 调用失败 | 9 |
| 上下文覆盖 equivalent / strict | 119/224 = 0.531 / 72/224 = 0.321 |
| 引用覆盖 equivalent / strict | 85/154 = 0.552 / 41/154 = 0.266 |
| 耗时 p50 / p95 | 6.5 s / 37.0 s |
| token | 输入 1,824,849，输出 16,419 |

## 归因

1. 检索是主要瓶颈：有 gold 映射的题只有 53% 取到完整证据。取到 gold 时，extractive 答对 32/40、abstractive 答对 24/35；没取到时以答错和拒答为主。
2. 是否题过度拒答：75 题拒答 40 题，其中 23 题检索未命中、13 题无 gold 映射。参考答案为 No 的 15 题、Yes 的 12 题，所以问题不只是"没提到就是 No"。
3. 无解题硬答：45 道全员标注为无解的题，模型回答了 22 道。
4. 引用宽松与检索无关：检索命中时引用支持率 116/154 = 0.75，未命中或无映射时 92/127 = 0.72；平均每个回答引用 1.57 段。
5. 回答上下文的渲染重复：每段先给一行关键词摘要，再完整重复一遍正文，prompt 长度近乎翻倍。

## 修复顺序

1. 检索召回：引入 reranker、扩大召回数量
2. 回答约束：证据不足时必须拒答，每条引用都要能支撑它前面那句
3. 是否题：要求模型根据证据给出 Yes / No
4. 可观测性：失败记录保存模型原始输出，评测输出逐题进度

judge 的可信度要等 4.8 人工校准完成后才能确定，在此之前，以上 judge 口径的数字只作参考。

## Judge 人工校准包（2026-10-24）

从上述 300 题运行 `.tmp/qasper-answer-full-20261023221544` 确定性抽取 50 道有有效 judge 结果的已回答题。清单共 50 例，全部能在来源 `answers.jsonl` 中找到且均已实际作答并经 judge 判定；按答案类型 × judge 正误的样本数为：abstractive 7/7、boolean 6/6、extractive 6/6、none 6/6（correct/incorrect）。

审核包位于 `.tmp/qasper-judge-calibration-20261024012827`，包括含完整题目、参考答案、系统回答、引用原文与 judge 理由的 `calibration.md`，机器可读的 `calibration.json` 和空白 `labels.template.csv`。空白 `labels.csv` 已由模板创建，等待人工填写；该文件没有被当作人工标签，也未将 4.8 标记为完成。工件 SHA-256：

| 工件 | SHA-256 |
|---|---|
| `calibration.json` | `d7080d20dda63a23b6064838a4dd87733bd6e51e9cc38b8c4f3279aed3efdea1` |
| `calibration.md` | `9fb7df65248efadace651baf8ab84b70c5ccf326ea5d6083ced4ce5e83929e4c` |
| `labels.template.csv` | `b831c16a1957114f5f9b1aa0c876b61243c4d51338db1a5d7e995b76a0a3e1b3` |

这些逐题审核工件保留在 Git 忽略的本地工件目录，版本控制只记录摘要和哈希。人工填写后，以同一目录中的 `labels.csv` 运行 `python -m scripts.dev rag-eval qasper --dataset-path F:/datasets/qasper/train-dev/qasper-dev-v0.3.json --cache-path F:/datasets/qasper/cache/qasper_dev_text-embedding-v4_1024.npz answer calibrate score --calibration-dir .tmp/qasper-judge-calibration-20261024012827` 生成一致率、Cohen's kappa、误判方向和引用级一致率；在该步骤完成前不得声称 judge 已校准。

## 检索召回实现进展（2026-10-24）

新变更 `qasper-answer-retrieval-recall` 已把 answer evaluation 的检索 profile、可选字段索引、轻量 reranker 注入和候选/上下文预算身份写入代码与报告。默认仍是 `chunk_only`；`field_only` 显式启用字段索引，`field_lightweight` 另外注入 reranker，由所选 `RetrievalPolicy` 按当前 query intent 决定调用。空 intent allowlist 表示全部允许，与共享策略语义一致。字段缓存不完整会在首题前失败，不会静默退回 chunk-only。

同一离线缓存已核查：17,048 个 chunk、32,302 个不同的非空字段文本、45,068 条向量缓存记录，缺失字段向量数为 0。固定 300 题的四组 retrieval-only replay 已完成，并经第二次运行核验逐题结果一致；完整工件身份、复现命令与检查结果见 [检索召回对照证据](../qasper-answer-retrieval-recall/evidence.md)。

扩大候选并加入字段索引后，候选 equivalent 覆盖达到 215/224（96.0%），但最终上下文最好仅为 110/224（49.1%）；本次 replay baseline 为 106/224（47.3%）。轻量 reranker 使字段方案降至 101/224（45.1%），因此没有提升为默认。该对照关闭 generation，不能与原回答运行 119/224（53.1%）的上下文覆盖直接比较，也不改变原 300 题的端到端正确率 50.9%。原始报告、回答、样本和原 query cache 的 SHA-256 均保持不变。

## 第二轮回答运行与故障恢复核验（2026-10-24）

后续提交 `bd2ae6e2` 修复实际检索条数与跨轮上下文选择，`d83788e2` 将评测切到桌面阅读问答的上下文运行时。第二轮原始 300 题运行的 equivalent/strict context coverage 为 139/224（0.621）和 99/224（0.442），原始端到端正确率 130/278（0.468），同时存在 38 个系统失败及 22 个 judge 失败（20 个调用错误、2 个非 JSON 输出）。

26 道 `model_configuration` 题另行重跑，25 道恢复；用全部这 26 条记录替换后，排除两轮任一系统失败或无判分题，251 道共同有效题的正确数为 131→139，错变对 26、对变错 18，连续性校正 McNemar p≈0.291。引用逐条支持率 0.740→0.739，无解题正确拒答均为 20/41。原始汇总、恢复后配对、micro/macro 和不同可答定义的分母必须区分。

完整来源哈希、离线配对分析、成本和限制见 [可观测性与生成约束证据](../qasper-answer-observability-and-grounding/evidence.md)。原 judge 人工校准待办仍未完成；历史工件未保留的 provider 原错误不能事后猜造。
