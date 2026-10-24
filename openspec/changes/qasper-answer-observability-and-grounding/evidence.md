# 第二轮 QASPER 评测与恢复证据（2026-10-24）

## 来源与分析规则

本记录承接其他模型完成的运行，主控重新读取本地原始工件核验。历史目录保持不变：

| 运行 | 目录 | 代码 revision |
|---|---|---|
| 修复前 300 题 | `F:/github/NewsRoom/.tmp/qasper-answer-full-20261023221544` | `c9f90d37607a3f3b815412e3e4b390da58918d67` |
| 修复后原始 300 题 | `F:/github/NewsRoom/.tmp/qasper-answer-full-20261024091735` | `d83788e2872ec04d232b1172ecbdafc8740677cf` |
| 26 道回答故障恢复 | `F:/github/NewsRoom/.tmp/qasper-rerun-model_configuration-1792823652` | `d83788e2872ec04d232b1172ecbdafc8740677cf` |

26 道恢复题的 ID 与原始第二轮全部 `model_configuration` 题严格一致；逐题 paper/question/stratum/reference 身份相同，25 题恢复为非系统失败，1 题仍为 provider failure。分析采用全部恢复尝试，不按照答案正确性选择。

先用这 26 条恢复记录替换原始第二轮对应记录，再排除任一轮为系统失败或已回答但无法判分的题。剩余 251 题，排除 49 题；两轮使用同一题目顺序。逐题清单、排除原因、原始指标和来源哈希保存于：

- `F:/github/NewsRoom/.tmp/qasper-answer-paired-analysis-20261024203533/comparison.json`
- SHA-256：`0bffd258a059e1999cb69782d01905fd59215a6587c3ea38bb30aa6bb83f4874`

这份离线分析不调用回答模型、judge 或 embedding，也不把手工恢复后的比较冒充原始 300 题运行报告。

## 核验结果与分母

| 指标 | 修复前 | 修复后（26 道恢复后配对） |
|---|---:|---:|
| 端到端正确 | 131/251 = 0.522 | 139/251 = 0.554 |
| judge 判已回答题正确 | 92/168 = 0.548 | 98/165 = 0.594 |
| 引用支持率（逐引用 micro） | 194/262 = 0.740 | 266/360 = 0.739 |
| 引用支持率（报告默认逐回答 macro） | 0.790 | 0.788 |
| 每个回答引用数 | 262/168 = 1.56 | 360/165 = 2.18 |
| 至少一名标注员认为可答的题中过度拒答 | 63/210 = 0.300 | 66/210 = 0.314 |
| 所有标注员认为可答的题中过度拒答（报告默认） | 44/174 = 0.253 | 45/174 = 0.259 |
| 全体标注无解的题正确拒答 | 20/41 | 20/41 |

上表同时记录用户对比口径和现有报告口径，避免将 micro/macro 或“任一/全体标注可答”混为同一个指标。现有评分定义未改变。

26 题由错变对，18 题由对变错。连续性校正 McNemar 统计量为 `(abs(26-18)-1)^2/(26+18) = 1.1136`，自由度 1 的卡方近似 p=0.2913，未达到常用 0.05 阈值。按抽样类型，extractive 正确数 37→46，boolean 18→23，abstractive 32→28，none 44→42。

原始整轮指标另列，不使用配对分母替代：

| 原始运行指标 | 修复前 | 修复后 |
|---|---:|---:|
| 端到端正确 | 148/291 = 0.509 | 130/278 = 0.468 |
| equivalent context coverage | 119/224 = 0.531 | 139/224 = 0.621 |
| strict context coverage | 72/224 = 0.321 | 99/224 = 0.442 |
| 输入 tokens（报告记录） | 1,824,849 | 3,290,621 |
| 端到端耗时 p50 | 6,547.6 ms | 11,208.9 ms |
| 系统失败 | 10/300 | 38/300 |
| judge 失败 | 9 | 22 |

第二轮 38 个系统失败包括 26 个 `model_configuration`、8 个 `model_malformed_output`、3 个 `model_provider_unavailable` 以及 1 个 halted。judge 的 22 个失败包括 **20 个 `judge_call_failed: LLMProviderError` 和 2 个 `judge_output_not_json`**，不能仅把 20 个调用失败当作全部 judge 失败。

26 题恢复还额外记录 718,186 个输入 tokens；原第二轮加这次回答恢复的已记录输入量合计 4,008,807。原始完整运行耗时与选中答案的 token 统计不能代表故障恢复的累计费用；失败请求未返回的 usage 仍可能不可得。

## 结论与限制

上下文覆盖改善、配对正确率小幅上升，与检索修复有效的方向一致，但当前证据没有证明统计显著的回答质量提升。引用支持率没有改善，无解题正确拒答未改善，更多上下文和引用未充分转化为正确答案。

26 题重跑恢复支持“先前故障具有临时性”，但历史工件没有保存原始 provider 错误类型和消息。无法追溯网关当时究竟返回了无效 JSON、缺失 choices、认证错误或其他响应；新代码也不能倒推出这些历史信息。不能将新分类规则应用到不存在的原始诊断上伪造归因。

旧 judge 人工校准任务 4.8 仍未完成。50 题审核包保留于 `F:/github/NewsRoom/.tmp/qasper-judge-calibration-20261024012827`，`labels.csv` 仍为空白。本次比较的 judge 结果仍须保留该限制。

## 历史工件 SHA-256

| 工件 | 修复前 | 修复后原始 | 26 道恢复 |
|---|---|---|---|
| `report.json` | `831c8de9b542ee3be556dcc224bdc87a39c02a36431396c561d7148fac6b5a64` | `51ca59c8b08bec4a777e86d865b4f845f30be5920b7d609ae438c5e98fe30779` | `ed50af0ae8ebddfe778333bfaccba87134944bd0af459a69f4c285c5ede32905` |
| `answers.jsonl` | `893b62dad3cf2dbb80cfdf1c5a68c0da17a97ecc3c8aabe7b39ba67fa674ac40` | `978e7ec80942b9c6ff43336976333badd9acca913e0c400e02ab47a82037e696` | `1262593cd0f1731f877f3ce5831e5edc2dd93d5f62cbf6c1df46bfffa76c32f1` |
| `sample.json` | `6d169d937bbba321eac4f0484dcebd827de69b2c3afd54c889b857b6bb6ea429` | `6d169d937bbba321eac4f0484dcebd827de69b2c3afd54c889b857b6bb6ea429` | `94277b0e2b8842bf3fdc20351c7b17c7476bd3a9dbf5d538b1a6896f6445eb13` |

## 本变更验收

### 实现与入口

- 回答错误经 `PaperAnswerModelError`、`AnswerWorkerError` 和既有 Harness transcript/decision 写入 `generation_error_detail`。模型协议错误保留实际失败输出的脱敏限长摘要；可识别的网关响应形状错误归为 provider unavailable，而认证、无效请求、模型不存在等仍保留配置分类。
- provider 结构化错误正文先递归脱敏嵌套对象、列表与字符串，再序列化和限长。HTTP 503 与 HTTP 200 错误正文回归覆盖凭据 assignment、PEM 与嵌套 message/details，避免原始摘要绕过安全投影。
- 回答评测逐题输出 START/END，judge 恢复逐题输出 before/after；进度立即写 stderr，stdout 仍为单条 JSON。此处没有新增 checkpoint 或中断续跑协议。
- 新回答工件保存完整 `judge_request`、schema/prompt 版本与 digest，以及回答模型、judge 模型的安全配置身份。judge 恢复冻结回答、上下文、引用、Answer-F1 和 coverage，仅更新判定、错误详情及派生统计，并另记新增 judge 调用和耗时；judge usage 未返回时明确为 unknown。
- 回答失败恢复只重跑源运行的系统失败。prepare、运行前预检和 merge 校验样本、配置、父工件哈希及逐题来源；不重答只有 judge 失败的题，不覆盖正常回答。新目录保留完整样本、失败的重试和累计已记录 token 成本。
- 生成提示版本为 `grounded_minimal_citations_v1`：事实后最少必要引用，每段独立支持相邻断言；证据不足使用既有拒答协议；Yes/No 需要正向或明确反向证据。结构 gate 及 Harness 预算保持原职责，提示测试不等于已证明语义质量提升。
- 评测 context 事件使用同级 `<output>.context/events.sqlite3` 持久化，通过既有 canonical durable event port 写入；实际事件写入后重新打开 port 的回归验证了同 run 可读、异 run 隔离及 snapshot 目录绑定。

新格式、已保存请求快照的工件，可以只构造 judge 进行恢复：

```powershell
python -m scripts.dev rag-eval qasper answer rejudge --source-run-dir <source-run> --output-dir <new-child-run>
```

`NEWS_RAG_EVAL_JUDGE_MODEL` 必须明确配置，当前 judge 身份须与源运行一致。历史工件没有快照时，在 `qasper` 后、`answer` 前提供 `--dataset-path` 和 `--cache-path`，由原资产重建请求；历史 judge model 缺失时还须显式提供 `--legacy-judge-model <confirmed-historical-model>`。该声明只建立 model 层面的比较，不证明历史 provider、部署与完整配置；本轮没有代替操作者猜填该值。

针对实际第二轮 22 道 judge 失败的恢复入口如下。`<confirmed-historical-judge-model>` 必须替换为确认过的历史模型；此命令会产生真实 judge 调用，本次开发未执行：

```powershell
$historicalJudgeModel = "<confirmed-historical-judge-model>"
$env:NEWS_RAG_EVAL_JUDGE_MODEL = $historicalJudgeModel
python -m scripts.dev rag-eval qasper `
  --dataset-path F:/datasets/qasper/train-dev/qasper-dev-v0.3.json `
  --cache-path F:/datasets/qasper/cache/qasper_dev_text-embedding-v4_1024.npz `
  answer rejudge `
  --source-run-dir F:/github/NewsRoom/.tmp/qasper-answer-full-20261024091735 `
  --output-dir F:/github/NewsRoom/.tmp/qasper-answer-rejudge-20261024 `
  --legacy-judge-model $historicalJudgeModel
```

输出目录必须不存在，源运行始终保持不变。默认选择包含 20 个调用失败和 2 个解析失败；恢复后的统计仍须保留人工 judge 校准未完成的限制。

回答系统失败恢复与合并的入口是 `answer run --rerun-failed-from <source-run>` 和 `answer merge --base-run-dir <source-run> --rerun-run-dir <recovery-run> --output-dir <new-child-run>`。前者仍需 dataset/cache/query-vector/output 配置。历史工件若缺少必填测量身份，或新 prompt 与历史配置不同，会被明确阻断；本变更没有把先前 26 条历史恢复伪装成已通过新严格 merge 合同的运行。

### 验证记录

真实第二轮工件的只读重建检查：300 条记录、22 道已回答但 judge 失败的题，22 道全部可以按原 valid-marker 顺序重建 `JudgeRequest`；调用模型 0 次，发布回答报告 0 份。资产 dataset/mapping/cache 身份全部匹配，原问题保留空白。检查发现并修复了两题尾空格与 frozen mapping 去空格规则不一致导致的误拦，回归测试保留该边界。

- 检查摘要：`F:/github/NewsRoom/.tmp/qasper-rejudge-structural-preflight-20261024.json`
- SHA-256：`8f64e15051bd83847c2fd2444749efe4926c9c98966bae70e34c34ead57b16d9`
- 这只是请求与资产的结构核验，**不代表历史 judge 完整配置已验证**。原 report 没有 judge model 身份，也没有历史 corpus/index 哈希；对应限制仍明确保留。

首次完整 smoke 发现 6 项失败：两项 LLM client fake 缺真实配置合同、三项 legacy fixture 的历史 judge 身份不一致，以及评测 context composition 使用内存事件日志的架构违规。前五项修正测试依赖或 fixture 的真实合同，最后一项改为真实 SQLite durable event store；没有弱化断言、跳过失败测试或修改生产代码迎合 fake。

最终审阅另补齐回答模型身份的 `temperature` 存在性校验：缺字段不等同显式 `None`，两侧同时缺失也拒绝。补充回归通过后重新冻结代码，并从头重跑检查。

最终冻结代码后的范围检查：所有 `test_qasper*.py`，以及 answer model/client/generator/adapter、Harness generation phase、`tests/framework/llm` 与 `tests/architecture`，合计 **909 passed**，8 条现有 FastAPI lifecycle 弃用 warning，无失败；日志为 `F:/github/NewsRoom/.tmp/qasper-observability-tests-final-20261024.log`。`python -m scripts.dev compile`、本变更与原 `qasper-end-to-end-answer-evaluation` 的 OpenSpec strict validation 均通过。

最终完整执行 `python -m scripts.dev smoke`，退出码 0：pytest 为 **5392 passed、1 skipped、23 deselected**，61 条现有 FastAPI/Starlette 弃用 warning，耗时 1589.85 秒；随后 AgentLoop 离线 fixture 成功且 `network_calls=0`，source validation 为 `is_valid=true`、错误和 warning 均为 0。保留项目既有离线选择条件，没有新增跳过项或弱化断言。完整日志为 `F:/github/NewsRoom/.tmp/qasper-observability-smoke-final-20261024.log`。

本变更不重新运行 live 回答或 judge，不以新分数宣称质量改善。三组历史 report/answers/sample 的九个 SHA-256 已重新核对，均与本记录相同。
