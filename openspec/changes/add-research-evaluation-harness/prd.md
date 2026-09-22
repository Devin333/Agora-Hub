# Research 评测体系产品需求文档（PRD）

## 1. 文档信息

| 项目 | 内容 |
| --- | --- |
| 产品名称 | Agora Hub Research Evaluation Harness |
| OpenSpec change | `add-research-evaluation-harness` |
| 文档状态 | Proposed |
| 文档类型 | 产品需求文档（PRD） |
| 目标使用者 | Research 工程师、Harness 工程师、发布评审人、CI 维护者 |
| 关联能力 | `research-evaluation-harness` |
| 生产链路 | `source collection -> evidence -> agent analysis -> report -> quality gate -> artifacts/storage` |

本文档定义“如何评测项目”的产品需求。它描述评测系统应该解决什么问题、提供什么能力、输出什么结果以及如何验收；具体模块划分和实现策略见同一 change 下的 `design.md`，可执行需求见 `specs/research-evaluation-harness/spec.md`。

## 2. 一句话概述

为现有 Research 运行链路建立一套版本化、可重复、确定性优先的评测体系：既评估报告是否正确、完整、有证据支持，也评估 Harness 是否遵守 `PLAN -> EXECUTE -> VERIFY`、预算和权限边界，并为每次运行生成可追溯、可比较、可诊断的评测产物。

## 3. 背景与现状

### 3.1 当前系统已有能力

仓库中已经存在若干与评测相关的能力，包括 evidence 评测、RAG Golden Set、citation 覆盖、实时答案评测以及 CI promotion gate。它们能够覆盖部分检索、答案和引用问题，但使用入口、数据契约和报告形式并不统一。

项目同时要求 Research 和 Harness 遵循以下架构约束：

- 运行路径应保持为 `source collection -> evidence -> agent analysis -> report -> quality gate -> artifacts/storage`。
- 确定性工作由普通函数、service 或 deterministic gate 完成，不交给 LLM 决定。
- Harness 是流程控制者，LLM 只生成候选内容。
- Harness 执行必须是有界的 `PLAN -> EXECUTE -> VERIFY` 状态机。
- `VERIFY` 必须由 deterministic gate 完成；失败时只能进行受控 retry、replan 或 halt。
- 每次 phase transition 都必须写入 durable transcript 或 event log。
- LLM 不得决定 workflow routing、quality pass/fail、memory write、tool authorization、publication 或 skill promotion。

### 3.2 当前评测缺口

当前缺少一个面向完整 Research 运行链路的统一评测产品，导致以下问题：

1. 只看最终报告文本时，无法知道事实是否来自有效 source 和 evidence。
2. 结果质量可能提高，但 citation、traceability 或 source independence 同时退化。
3. Harness 的 retry、replan、turn budget 和 halt 行为没有统一的 case 集合验证。
4. worker 生成越权候选内容时，缺少自动化检查证明 Harness 没有授予实际副作用权限。
5. 失败通常只能看到最终错误，无法定位到 collection、evidence、analysis、VERIFY 或 storage 阶段。
6. 不同代码、prompt、模型或配置变更无法和同一份基线直接比较。
7. 真实外部 source 的波动会污染本应确定性的回归测试。

### 3.3 为什么现在做

项目已经积累了 evidence、RAG、Harness 和 artifact 相关基础能力，适合在现有契约之上补齐统一评测层。若先继续优化模型或业务流程，再补评测，后续很难判断改动带来的真实收益和隐性回归。因此本 change 的首要产物不是“追求一个漂亮分数”，而是建立第一份可信、可重跑、可解释的质量基线。

## 4. 产品目标

### 4.1 总体目标

建立可服务于本地开发、Pull Request、定时运行和发布评审的 Research Evaluation Harness，并完成第一轮基线评测。

### 4.2 可量化目标

- 建立一个版本化的 50-case 初始 Golden Set。
- 对每个 case 产出确定性的通过/失败结论、指标、失败阶段和证据引用。
- 验证 `PLAN -> EXECUTE -> VERIFY` 的合法 transition、retry、replan、budget 和 halt 行为。
- 让每次评测都生成完整 run artifact，可用于 replay、review 和 baseline comparison。
- 让快速评测可以在不依赖外部服务的情况下运行并返回稳定的 CI exit code。
- 将内容质量、证据质量、Harness 正确性和运行成本分开统计，避免单一总分掩盖严重问题。

### 4.3 成功定义

当工程师能够使用同一个 runner 选择数据集和 case，重复运行得到一致的 deterministic 结果；当某个 case 失败时，报告能够指出具体阶段、断言、source/evidence/artifact 引用和修复方向；当代码或模型发生变化时，系统能够和历史 baseline 比较并标出新增失败，评测体系即达到第一阶段成功标准。

## 5. 非目标

第一阶段明确不做以下工作：

- 不训练、微调或自动选择新的模型 provider。
- 不替换、重构或绕过现有 Research production runtime。
- 不把 LLM judge 作为唯一 quality gate，也不允许 judge 覆盖 deterministic failure。
- 不让正常业务 run 直接更新 active skill、memory、policy 或 production version。
- 不要求 deterministic Harness 场景访问真实外部 source、LLM、Postgres 或 Qdrant。
- 不在第一阶段开发新的评测 Web UI。
- 不把所有 live source 变化强行纳入 Pull Request 阻断门禁。
- 不为了评测保留当前架构明确要求删除的 compatibility layer 或 legacy shortcut。

## 6. 目标用户与使用场景

### 6.1 Research 工程师

**核心诉求：** 判断报告结果是否真正正确、完整、有依据，并能快速定位回归。

**典型场景：**

- 修改 evidence extraction 后，运行 `research-001` 到 `research-010`，确认 requirement coverage 和 citation coverage 没有下降。
- 某个报告出现事实错误时，打开 `failures.jsonl`，查看 claim、evidence、source 和失败阶段。
- 修改 source ranking 后，对比 baseline，确认 source quality 提升没有导致答案覆盖率下降。

### 6.2 Harness 工程师

**核心诉求：** 验证流程控制、预算和权限边界，而不是依赖自然语言判断。

**典型场景：**

- 注入一个 invalid worker output，确认 Harness 进入受控失败路径。
- 让 VERIFY 连续失败，确认达到 retry 或 `max_replans` 后 halt，且不会继续 publication。
- 注入 tool timeout 或 storage failure，确认 transcript 和 failure taxonomy 完整。

### 6.3 发布评审人

**核心诉求：** 判断某次代码、prompt、模型或配置变化是否可以发布。

**典型场景：**

- 查看 baseline comparison 中的 metric delta、new failures 和 critical failures。
- 确认总体平均分通过时，没有被 unsupported critical claim、fabricated citation 或 gate bypass 否决。
- 审阅需要人工复核的 semantic review 样本。

### 6.4 CI 维护者

**核心诉求：** 在 Pull Request 中稳定、低成本地执行确定性回归。

**典型场景：**

- 使用离线 fast tier，不依赖 live source 或外部模型服务。
- 依据非零 exit code 阻断明确的 schema、traceability、budget 和 architecture regression。
- 让 scheduled tier 单独承担 live corpus 的 latency、cost 和 source 波动。

## 7. 产品原则与架构约束

### 7.1 确定性优先

能通过结构、引用、状态机、预算、文件和 import 关系判断的内容，必须由 deterministic evaluator 判断。LLM judge 只能评估相关性、完整性、表达清晰度等语义维度。

### 7.2 评测不改变生产行为

评测代码通过 application service 和结构化 API 调用运行链路，不直接进入 executor、store 或底层框架内部。评测 adapter 可以替换依赖，但不能改变 production routing 或 side-effect authority。

### 7.3 失败必须可归因

每个失败必须至少关联 `case_id`、`phase`、`failure_category`、`assertion`、相关 source/evidence/artifact 引用和可读的修复提示。只有“总分下降”而没有原因的报告不满足产品要求。

### 7.4 严重问题一票否决

fabricated source、关键 claim 无 evidence、gate bypass、budget violation、缺失 durable transition 和越权副作用不能被平均分抵消。

### 7.5 数据和运行不可变

dataset、evaluator、run 都必须有版本或唯一 ID。评测产物采用 append-only 方式写入，重复运行生成新的 `run_id`，不覆盖历史 baseline。

## 8. 产品范围

### 8.1 本次交付范围

- `EvaluationCase`、dataset manifest、`EvaluationRun`、metric、failure 和 semantic review 的版本化契约。
- 50 个首批 Golden Set case。
- 内容、证据、citation、traceability、Harness、架构和 artifact 的 deterministic evaluator。
- source、worker、tool、verifier 和 storage 的 fake adapter。
- 本地 runner、case 选择、dataset 选择、run ID、JSON/Markdown 输出和 baseline comparison。
- fast PR tier、scheduled tier 和 release tier 的入口定义。
- 评测报告、失败分类、指标门禁和人工复核记录。

### 8.2 不在本次交付范围

- 新的 production API。
- 新的业务 source connector。
- 自动修复报告内容。
- 自动发布报告或自动 promotion skill。
- 面向终端用户的评测可视化页面。

## 9. 完整产品流程

```text
选择 dataset/case/tier
        |
        v
校验 EvaluationCase 与运行配置
        |
        v
执行 Research runtime 或 deterministic fake runtime
        |
        v
收集 source -> 生成 evidence -> 运行 analysis -> 生成 report
        |
        v
执行 deterministic quality gate 与 Harness checks
        |
        +--> 可选 semantic review（不能覆盖 deterministic failure）
        |
        v
聚合指标、应用阈值、执行 critical-failure veto
        |
        +--> 可选 baseline comparison
        |
        v
写入 run artifacts 并返回 CI exit code
```

### 9.1 运行前

runner 接收 dataset version、case IDs、tier、run ID、baseline run 和配置。它必须先校验 manifest，不允许在数据契约错误时调用 Research runtime。

### 9.2 运行中

fast tier 使用固定 evidence 和 fake adapter，scheduled/release tier 可以使用真实 application service 和 captured/live source。所有阶段都必须保留稳定的 case 引用和可追踪的 artifact 引用。

### 9.3 运行后

deterministic evaluator 先执行；如果存在 critical failure，case 立即失败。semantic reviewer 可以继续提供辅助分数，但不能把失败改为通过。runner 汇总 per-case、aggregate 和 baseline delta，写入完整报告后返回退出码。

## 10. 功能需求

### FR-EVAL-001：评测 case 与 dataset 管理

每个 case 必须包含以下字段：

```yaml
id: research-001
schema_version: "1"
category: normal-research
query: "用户原始问题"
as_of_date: "2026-09-22"
expected_requirements:
  - id: requirement-1
    description: "必须回答的事实点"
    critical: true
required_source_types:
  - primary
  - authoritative_secondary
forbidden_claims:
  - "不能出现的错误结论"
minimum_sources: 3
risk_level: medium
```

要求：

- `id` 在同一 dataset version 内唯一且稳定。
- `schema_version` 必须被 manifest 声明并可校验。
- `expected_requirements` 必须支持 critical 标记。
- `required_source_types`、`forbidden_claims` 和 `minimum_sources` 用于 deterministic check。
- 可选字段必须支持 expected behavior、source snapshot、fixture、threshold 和人工 rubric 引用。
- 缺字段、重复 ID、未知 category、无效 schema version 必须在执行前失败。

### FR-EVAL-002：首批 Golden Set

首批 dataset 共 50 个 case，分类和最低数量固定如下：

| category | 数量 | 目的 |
| --- | ---: | --- |
| `normal-research` | 20 | 检查正常 Research 结果的覆盖率、相关性和报告质量 |
| `evidence-citation` | 10 | 检查引用、证据支持、source quality 和 traceability |
| `harness-failure` | 10 | 检查 retry、replan、budget、timeout、halt 和 transcript |
| `boundary-security` | 10 | 检查 prompt injection、越权、冲突来源和架构边界 |

dataset evaluator 必须报告各分类数量；任何分类低于要求都使 dataset gate 失败。

### FR-EVAL-003：确定性内容评测

确定性 evaluator 至少支持：

- 输出 schema 合法性。
- `expected_requirements` 覆盖率。
- critical requirement 是否满足。
- 关键 factual claim 是否有对应 evidence。
- `forbidden_claims` 是否出现在最终报告。
- 最少 source 数量和 source type 是否满足。
- 报告是否存在必要的结构和 artifact reference。

如果 critical requirement 未满足、forbidden claim 出现或报告 schema 无效，case 必须失败；不能仅依靠平均分判断。

### FR-EVAL-004：证据与 citation 评测

evaluator 必须检查：

- citation 是否存在并能解析。
- citation 是否指向当前 run 的有效 source 或 evidence。
- citation 内容是否真正支持对应 claim。
- 需要 citation 的 claim 是否都具备 citation。
- report -> evidence -> source 的 lineage 是否完整。
- source 是否满足 required source type、时间和最低数量约束。
- 多个 source 是否只是同一内容的重复转载（如果数据契约提供独立性信息）。

以下情况必须触发 critical failure：

- 虚构 source 或 citation。
- 关键 claim 没有 evidence。
- citation 存在但实际不支持 claim。
- report 中的关键结论无法追溯到 source。

### FR-EVAL-005：Harness 状态机评测

evaluator 必须验证：

- phase transition 符合 `PLAN -> EXECUTE -> VERIFY` 以及允许的 retry/replan/halt 分支。
- `VERIFY` 由 deterministic gate 执行。
- VERIFY 失败时只执行允许的 retry 或 replan。
- `max_replans`、`max_turns` 和 retry budget 得到实际执行。
- budget 耗尽后运行进入 terminal failure 或 halt。
- halt 后不得继续 publication、memory write、skill promotion 或其他副作用。
- 每次 transition 都产生 durable transcript/event record。

### FR-EVAL-006：可控故障场景

必须提供与 application contract 对齐的 fake adapter，并覆盖以下场景：

| 场景 | 预期验证点 |
| --- | --- |
| invalid plan | 计划 schema 校验、受控失败和 transcript |
| invalid worker output | worker 候选不得直接进入副作用路径 |
| source timeout | source failure taxonomy、停止伪造 evidence |
| partial collection failure | 已收集与缺失 source 的边界 |
| tool timeout | retry policy、失败阶段和预算 |
| conflicting sources | 冲突记录、abstain 或受控报告 |
| VERIFY retry then pass | 失败 transition、重试次数和最终通过 |
| VERIFY budget exhausted | terminal halt、无 post-halt side effect |
| storage write failure | artifact failure、可诊断错误和最终状态 |
| evidence prompt injection | candidate 与 authority 分离、拒绝越权请求 |

这些场景不得依赖真实外部 service，重复运行必须得到相同结果。

### FR-EVAL-007：语义评审

系统可以接入 human reviewer 或 LLM judge，对以下维度评分：

- 是否回答用户问题（relevance）。
- 是否覆盖重要事实和限制（completeness）。
- source 是否权威、独立、适合当前问题（source quality）。
- 报告是否清晰、结构是否可读（clarity）。

语义评审记录必须包括：

- `reviewer_type`。
- `reviewer_id` 或 model identifier（可用时）。
- `rubric_version`。
- 评审时间。
- 每个维度的分数和理由。
- 与对应 `case_id`、`run_id` 的关联。

semantic review 是 advisory result；它不能覆盖 deterministic failure，也不能决定 workflow routing、quality gate、publication、memory write 或 skill promotion。

### FR-EVAL-008：评测运行产物

每次运行必须写入唯一目录：

```text
evaluations/runs/<run_id>/
  manifest.json
  cases.jsonl
  outputs.jsonl
  metrics.json
  failures.jsonl
  summary.md
```

`manifest.json` 至少记录：

- `run_id`。
- repository revision。
- dataset version。
- evaluator version。
- execution tier。
- 运行配置和阈值。
- model/provider metadata（可用时）。
- start/end timestamp。
- runner version。

`cases.jsonl` 和 `outputs.jsonl` 应能关联：

- 原始 query。
- source 和 evidence 引用。
- plan。
- phase transitions。
- worker candidate。
- verification result。
- final report。
- latency、call count、token 和 cost signal（可用时）。

`failures.jsonl` 必须记录 case、phase、failure category、assertion、observed value、expected value、关联 artifact 和修复线索。

### FR-EVAL-009：指标、阈值与 baseline comparison

runner 必须生成 per-case 和 aggregate 指标，并支持与历史 baseline 比较：

- 指标 delta。
- 新增失败 case。
- 恢复通过的 case。
- critical failure 变化。
- latency、call count 和 cost 变化。
- 触发的 threshold 和具体 observed value。

baseline comparison 必须记录比较双方的 run ID、dataset version 和 evaluator version；dataset 或 evaluator version 不一致时必须显式提示，不能静默比较。

### FR-EVAL-010：本地 runner 与执行层级

runner 必须支持以下输入：

```text
dataset version
case IDs
run ID
execution tier
baseline run
output directory
threshold configuration
```

建议命令形态如下，具体命令名以实现阶段与现有 `scripts.dev` 约定为准：

```powershell
python -m scripts.dev eval-research --dataset v1 --tier fast
python -m scripts.dev eval-research --dataset v1 --cases research-001,research-002
python -m scripts.dev eval-research --dataset v1 --baseline <run_id> --tier release
```

执行层级：

1. **Fast PR tier**：离线、确定性、无外部 service，用于 Pull Request。
2. **Scheduled tier**：更大 corpus，可以使用 captured/live source，用于观察质量、延迟和成本分布。
3. **Release tier**：完整 critical case、baseline comparison 和人工复核，用于发布前决策。

### FR-EVAL-011：架构与权限边界

评测实现必须检查并遵守：

- interface 只调用 application service，不直接调用 executor 或 store。
- `backend/research` 在重建期间不依赖 `backend/boards/paper_radar`、`interfaces` 或 `infrastructure`。
- worker 输出只作为 candidate data。
- Harness 负责 routing、quality pass/fail、memory write、tool authorization、publication 和 skill promotion。
- evaluator 使用结构化 API，不通过拼接 argv 调用另一个 CLI 完成功能。
- 评测自身不得引入会改变 production behavior 的兼容层或旁路。

## 11. 数据模型与契约

### 11.1 `EvaluationCase`

```text
EvaluationCase {
  id: string
  schema_version: string
  dataset_version: string
  category: enum
  query: string
  as_of_date: date
  expected_requirements: Requirement[]
  required_source_types: string[]
  forbidden_claims: string[]
  minimum_sources: integer
  risk_level: enum
  expected_behavior?: string
  source_snapshot?: string
  fixture_ref?: string
  thresholds?: ThresholdConfig
  rubric_ref?: string
}
```

### 11.2 `EvaluationRun`

```text
EvaluationRun {
  run_id: string
  dataset_version: string
  evaluator_version: string
  repository_revision: string
  tier: enum
  selected_case_ids: string[]
  config: object
  started_at: datetime
  completed_at: datetime
  status: enum
  deterministic_summary: object
  semantic_summary?: object
  baseline_ref?: string
}
```

### 11.3 `EvaluationFailure`

```text
EvaluationFailure {
  run_id: string
  case_id: string
  phase: enum
  failure_category: string
  severity: enum
  assertion: string
  expected: object
  observed: object
  source_refs: string[]
  evidence_refs: string[]
  artifact_refs: string[]
  critical: boolean
  remediation_hint?: string
}
```

## 12. 指标定义与首版目标

### 12.1 内容和证据指标

| 指标 | 口径 | 首版目标 | 是否阻断 |
| --- | --- | ---: | --- |
| Requirement Coverage | 满足的 required requirement 数 / required requirement 总数 | >= 85% | 是 |
| Factual Precision | 有有效 evidence 支持的 factual claim 数 / factual claim 总数 | >= 95% | 是 |
| Unsupported Claim Rate | 无充分 evidence 的 claim 数 / claim 总数 | <= 3% | 是 |
| Citation Validity | 可解析且指向有效记录的 citation 数 / citation 总数 | >= 98% | 是 |
| Citation Correctness | 实际支持对应 claim 的 citation 数 / citation 总数 | >= 95% | 是 |
| Citation Coverage | 已引用的需引用 claim 数 / 需引用 claim 总数 | >= 95% | 是 |
| Source Quality | 满足 authority/type/independence 规则的 source 比例 | 首轮基线 | 否，首轮观察 |
| Traceability | 具备完整 report-evidence-source lineage 的报告比例 | 100% | 是 |

### 12.2 Harness 和工程指标

| 指标 | 口径 | 首版目标 | 是否阻断 |
| --- | --- | ---: | --- |
| Invalid Transition Rate | 非法 transition 数 / transition 总数 | 0 | 是 |
| Budget Violation Rate | 超过 `max_replans`、`max_turns` 或 retry budget 的 run 比例 | 0 | 是 |
| Transcript Completeness | 有 durable record 的 transition 数 / transition 总数 | 100% | 是 |
| Gate Bypass Count | 未经 deterministic gate 即继续的 run 数 | 0 | 是 |
| Unauthorized Side Effect Count | worker 或 judge 获得越权副作用的次数 | 0 | 是 |
| Artifact Completeness | 具备全部必需 run artifact 的 run 比例 | 100% | 是 |

### 12.3 性能和成本指标

首轮只建立基线，不立即设置阻断阈值：

- `P50 latency`。
- `P95 latency`。
- source collection latency。
- analysis latency。
- verification latency。
- LLM call count。
- tool call count。
- input/output token count。
- source count。
- estimated cost。

scheduled tier 运行至少一周后，再依据真实分布确定 latency 和 cost 的发布阻断阈值。

## 13. 通过、失败与降级规则

### 13.1 Case 通过

case 只有在以下条件同时满足时才可标记为 `passed`：

- schema 校验通过。
- 必要 requirement 达到阈值。
- 没有 critical failure。
- 所有必需引用和 lineage 检查通过。
- Harness transition、budget 和 transcript 检查通过。
- 必需 artifact 已生成。

semantic review 未运行不影响 deterministic pass，但必须在结果中标记为 `not_reviewed`，不能伪造语义分数。

### 13.2 Case 失败

下列问题必须使 case 失败：

- 关键事实错误。
- fabricated source 或 citation。
- critical claim 无 evidence 支持。
- `report -> evidence -> source` 无法追溯。
- illegal phase transition。
- VERIFY 没有由 deterministic gate 执行。
- retry、replan 或 turn budget 被超出。
- halt 后继续执行或产生 publication、memory、skill 等副作用。
- 缺少 phase transition durable record。
- 评测路径引入 forbidden architecture dependency。

### 13.3 Run 失败

以下情况会使整个 run 返回非零 exit code：

- dataset manifest 无法加载。
- 任一 critical case 失败。
- aggregate metric 低于 configured threshold。
- baseline comparison 超出允许 regression threshold。
- run artifact 不完整。
- evaluator 自身发生未分类异常。

## 14. 报告要求

### 14.1 Markdown summary

`summary.md` 必须包含：

1. Run information：run ID、commit、dataset、evaluator、tier、时间。
2. Overall result：case 总数、通过数、失败数、critical failure 数。
3. Quality metrics：coverage、precision、citation、traceability。
4. Harness metrics：非法 transition、budget violation、transcript completeness、gate bypass。
5. Runtime metrics：P50/P95 latency、调用数、token、cost。
6. Failure categories：按 collection、evidence、analysis、VERIFY、storage、architecture 分类。
7. Baseline comparison：改善、退化、新失败、恢复通过。
8. Critical failures：逐条列出 case、阶段、断言和 artifact 引用。
9. Review status：semantic review 是否完成、人工抽样数量和一致性。

### 14.2 机器可读输出

`metrics.json` 用于 CI 和趋势分析；`failures.jsonl` 用于逐条诊断；`cases.jsonl` 和 `outputs.jsonl` 用于 replay 与审查。所有文件必须使用稳定字段名，并包含 `run_id` 和 `case_id` 关联。

## 15. 三档评测门禁

### 15.1 Pull Request 门禁

执行内容：

- compile 相关检查。
- deterministic case。
- Harness fake scenarios。
- citation 和 traceability evaluator。
- architecture boundary checks。
- 必要时运行 `python -m scripts.dev smoke`。

特征：无 live external service、结果可重复、目标运行时间可控制在 CI 可接受范围内。

### 15.2 Scheduled 评测

执行内容：

- 更完整的 Research corpus。
- captured/live source 评测。
- P50/P95 latency、token 和 cost 统计。
- source freshness、source failure 和 abstention 观察。

特征：结果主要用于趋势和诊断；外部 source 波动不能直接伪装成代码回归，报告必须记录环境和 source 时间。

### 15.3 Release 评测

执行内容：

- 全量 critical case。
- baseline comparison。
- 所有新增 failure 的人工复核。
- semantic judge 与人工样本校准。
- 发布前 artifact 归档。

特征：任何 critical failure、gate bypass、fabricated citation、traceability failure 或 budget violation 都阻断发布。

## 16. 验收标准

### 16.1 产品验收

1. 50 个首批 case 可以被版本化 manifest 加载。
2. 四个 dataset category 的数量满足 20/10/10/10。
3. 同一 deterministic case 重复运行得到一致结果。
4. 操作员可以按 dataset version 和 case IDs 选择运行范围。
5. runner 可以生成 JSON 和 Markdown 报告。
6. runner 可以与 baseline 比较并输出 metric delta 和 new failures。
7. fast tier 不依赖外部服务并返回稳定 exit code。

### 16.2 质量验收

1. 能检测 schema、requirement coverage、forbidden claim、citation、source 和 traceability 错误。
2. 能检测 illegal transition、VERIFY gate bypass、budget violation 和 post-halt side effect。
3. 能检测缺失 durable transcript。
4. 能检测 worker 或 judge 越权决定 publication、memory、skill、routing 或 quality gate。
5. 关键失败不会被 aggregate score 掩盖。
6. 每个失败都有 case、phase、category、assertion 和 artifact 引用。

### 16.3 工程验收

1. 必须生成 `manifest.json`、`cases.jsonl`、`outputs.jsonl`、`metrics.json`、`failures.jsonl` 和 `summary.md`。
2. 评测实现使用 application services 和 structured APIs。
3. 不改变现有 Research production path。
4. 不向 `backend/research` 引入禁止的 legacy 依赖。
5. 运行 `openspec validate add-research-evaluation-harness --strict` 通过。
6. 实现完成后运行 `python -m scripts.dev compile`、`python -m scripts.dev test` 和 `python -m scripts.dev smoke` 通过。

## 17. 分阶段交付计划

### Phase 1：评测核心

- 定义 case、dataset、run、metric、failure 和 semantic review schema。
- 实现 manifest loader 和 schema validator。
- 实现 deterministic evaluator、metric aggregation 和报告序列化。
- 建立最小 offline runner。

**阶段出口：** 能对固定 fixture 运行确定性评测，生成完整 artifact，并对阈值返回正确 exit code。

### Phase 2：Harness 场景

- 增加 fake source、worker、tool、verifier 和 storage adapter。
- 增加 invalid plan、invalid output、timeout、conflict、retry、replan、budget exhaustion 场景。
- 增加 transcript、terminal state 和 side-effect authority 检查。

**阶段出口：** 所有 Harness failure case 可离线重复运行，且失败原因和终态可验证。

### Phase 3：Golden Set 与首轮基线

- 编写并评审 50 个 Golden Set case。
- 运行首轮 baseline。
- 固定一批人工样本校准 semantic review。
- 记录初始 latency、token、call count 和 cost 分布。

**阶段出口：** 有一份可审查、可重跑、带 evaluator/dataset/version 信息的正式 baseline report。

### Phase 4：CI 与发布工作流

- 将 fast tier 接入 Pull Request。
- 建立 scheduled tier 和结果留存策略。
- 建立 release tier、critical failure 人工复核和发布检查单。
- 明确 dataset owner、evaluator owner 和失败处理流程。

**阶段出口：** 评测成为代码、模型和配置变更的稳定回归门禁。

## 18. 风险与应对

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| Golden Set 过时 | 指标不能反映真实用户问题 | dataset version、owner、review date 和定期复审 |
| live source 波动 | 把外部波动误判为代码回归 | fast tier 使用 captured evidence，live 只放 scheduled/release |
| 平均分掩盖严重问题 | 错误报告可能被放行 | critical failure veto 和逐条 failures artifact |
| LLM judge 漂移 | 语义分数不稳定 | judge 只做辅助，保留 rubric version 和人工校准样本 |
| artifact 体积增长 | 存储和检索成本增加 | retention policy，baseline/critical failure 长期保留 |
| 评测代码侵入生产路径 | 引入难以发现的行为变化 | application boundary、architecture test 和 import 检查 |
| metric 定义不一致 | 历史比较失真 | evaluator version、指标公式、dataset version 一起记录 |
| 评测自身异常 | CI 结果不可解释 | 分类 evaluator error、保留完整 run manifest、非零退出 |

## 19. 运营与责任边界

### Dataset owner

负责 case 内容、事实要求、source snapshot、review date 和版本发布。

### Evaluator owner

负责指标实现、阈值变更、失败分类、向后兼容和 evaluator version。

### Harness owner

负责状态机、budget、transcript、side-effect authority 和 fake scenario 的正确性。

### Release reviewer

负责 critical failure 复核、baseline comparison 审查和发布决策。

### CI maintainer

负责命令入口、exit code、Pull Request 门禁、scheduled job 和 artifact retention。

## 20. 开放决策

以下问题不阻塞第一版评测核心实现，但必须在 Phase 3 或 Phase 4 关闭：

1. 50-case Golden Set 的长期仓库归属目录是否统一放在现有 `data/eval/`，还是建立独立的 `evaluations/` 数据目录。
2. 评测 summary 是否需要写入现有 artifact store，还是第一阶段只保留文件系统产物。
3. scheduled tier 运行一周后，P95 latency、cost 和 token 是否需要成为发布阻断阈值。
4. semantic review 的人工抽样比例和最低一致率要求是多少。
5. live source 失败、abstain 和 source freshness 是否需要在首版 aggregate score 中单独展示。

## 21. 与实现任务的对应关系

本 PRD 对应同一 change 下的 `tasks.md`：

- “Contracts and Dataset” 对应第 10 节和 FR-EVAL-001、FR-EVAL-002。
- “Deterministic Evaluation Core” 对应 FR-EVAL-003、FR-EVAL-004、FR-EVAL-009 和第 12 节。
- “Harness Scenario Evaluation” 对应 FR-EVAL-005、FR-EVAL-006 和第 13 节。
- “Artifacts and Reporting” 对应 FR-EVAL-007、FR-EVAL-008 和第 14 节。
- “Tooling and CI” 对应 FR-EVAL-010、第 15 节和 Phase 4。
- “Verification and Rollout” 对应第 16 节和第 17 节。
