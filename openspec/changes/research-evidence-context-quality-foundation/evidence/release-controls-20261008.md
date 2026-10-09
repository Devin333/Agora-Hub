# Research quality foundation：启用边界与回滚

建立日期：2026-10-08；实现边界更新：2026-10-10。对应变更：`research-evidence-context-quality-foundation`。

## 交付边界

本轮交付 P0 输入合同、V1 Claim 证据资格、现有 CI 离线评测，以及 P1 默认关闭的检索候选试验。所有资格判断由 Research-owned deterministic resolver/gate 完成；Harness 继续拥有计划、路由、重试预算和发布权。

P2 单 run 上下文连续性尚未启用。`context-trigger-audit-20261008.md` 记录了本地触发核查；没有真实窗口丢失、条件漂移或重复读取成本的合格证据，不能把条件任务 6.6–6.12 视为已实现，也不能为了完成任务编号提前建立续接状态系统。

## 输入合同与 V1 Claim

`AnalyzePaperRequest.options` 的 `research_input_quality_enabled` 默认为 `False`，只接受布尔值。启用时必须同时使用现有动态单篇论文入口（`dynamic_analysis=True` 或 `dynamic_task_plan=True`），且部署环境已配置对应动态 runner。示例选项：

```python
options = {
    "dynamic_analysis": True,
    "research_input_quality_enabled": True,
    "research_input_source_revision": accepted_source_revision,
    "research_eligibility_gate_version": "ResearchClaimEvidenceEligibilityGate@1",
    "research_input_objective": "Analyze this paper using its cited evidence",
    "research_input_constraints": ["Preserve the dataset and evaluation split"],
}
```

`accepted_source_revision` 必须与实际载入的来源修订匹配，不能随意填写。也可传入 `research_input_profile`；application service 仍验证 tenant、paper、来源、来源修订、类别和状态。profile 必须恰好包含一个 `CURRENT_TASK`。已核验结论还需受信任的 qualification service 验证资格引用，缺失该服务时拒绝这类输入。无效 profile 或未知 gate 版本在 source/worker dispatch 之前被拒绝。最终模型请求中的完整输入投影必须保持其校验和。

V1 Claim 类型为 `numeric_comparison_result_v1`：同一表格、相同 Dataset/Split 下两个明确对象的 accuracy 百分比比较。它绑定原始来源、来源/证据修订、table/cell locator、claim revision、对象、指标、单位、source scale、适用条件及 resolver/gate 版本。自动提取表格需具备 `Method`、明确 Accuracy 百分比列、`Dataset`、`Split`，或以 `Unit=percent` 和 `Scale=percent|fraction` 明确单位与缩放。`0.92` 不会仅凭数值大小被猜测为 92%。缺失、歧义或条件冲突不能形成已核验结论。

启用路径使用独立、精确的 `research.paper_analysis.dynamic.quality-foundation.graph@1` 身份；run spec、context、RAG memory admission 和 recovery 选择同一 Graph 定义。`ResearchClaimEvidenceEligibilityGate@1` 依赖现有 `ClaimEvidenceGate@1`，同时验证原有引用和新资格。仅 `supported` 可以获得资格；`contradicted`、`insufficient_evidence`、`verification_unavailable` 均不能发布为已核验结论。gate 会重放 resolver 决策并与已经接受的 source/document/evidence pack 核对，不信任 worker 自报的 `passed`、`publish`、route 或 memory/tool 权限。

`insufficient_verified_progress` 及其指标仅用于诊断，不能独立改变路由、自动停机或批准发布。失败后的 retry/replan/halt 继续使用现有 Harness 有界策略。

## 检索候选试验

在 `PaperRAGSession` 注入 `RetrievalCandidateExperimentConfig`。默认配置为：

| 项目 | 默认值 |
| --- | --- |
| `enabled` | `False` |
| `max_candidate_queries` | `2` |
| `max_worker_calls` | `1` |
| `timeout_seconds` | `8.0` |
| `max_transport_attempts` | `1` |
| `allowed_expansion_terms` | 空 |
| `configured_hard_constraints` | 空 |

仅注入 `plan_worker` 不会启用试验。V1 候选保留完整原始查询，仅可追加明确允许的扩展词；实体、时间、来源、语言、版本、度量、否定和显式约束接受确定性检查。候选数量超限、非法控制字段、验证失败、worker 故障/超时或调用预算耗尽，均使用原始确定性计划。实际配置、候选血缘、校验结果和回退原因随计划记录。

默认关闭状态适用于部署配置及代码默认值。离线质量检查通过也不会修改配置；只有达到冻结收益阈值、且没有任何质量退化的评测结果，才支持另行提出有限启用范围。扩大范围必须记录适用查询集、来源、执行版本、负责人及回滚条件。

## 固定评测与门槛

复用已有命令和 `.github/workflows/ci.yml` 的 RAG CI gate：

```powershell
python -m scripts.dev test-rag-eval-gate
```

默认产物位于 `.newsroom/eval/ci/`，新增 `candidate_experiment_quality_report.json` 和对应 Markdown。评测仅使用现有固定语料与本地 worker fixture，无外部 source/model 服务。报告记录逐案例原因、输入/Claim 合同结果、原始计划回退、成对检索指标及案例、代码、配置校验身份。

| 版本/阈值 | 值 |
| --- | --- |
| candidate implementation | `research-retrieval-candidate-v1` |
| candidate configuration | `research-retrieval-candidate-config-v1` |
| fixed case set | `research-candidate-cases-v2` |
| input / Claim case set | `research-quality-foundation-cases-v2` |
| evaluation thresholds | `research-candidate-thresholds-v2` |
| case pass rate | `1.0` |
| input integrity | `1.0` |
| claim qualification | `1.0` |
| constraint preservation | `1.0` |
| fallback accuracy | `1.0` |
| worker control-field rejection | `1.0` |
| maximum quality regression | `0.0` |
| minimum rollout hit-rate gain | `0.01`（绝对增量） |

输入完整性和 Claim 资格的固定案例必须全部通过。基线和候选分别执行现有固定语料检索，对比 hit rate、evidence coverage、required type coverage、source locator coverage、citation accuracy 和 MRR。缺失指标或任一退化导致质量 gate 失败；收益未达门槛时 `rollout_eligible=False`，即使 `passed=True` 也保持默认关闭。

## 回滚与审查

1. 对新请求关闭 `research_input_quality_enabled`，恢复现有未启用输入合同的路径；保持当前请求的输入和 Graph identity 不变，不把运行中的 opt-in run 强行切换 Graph。
2. 将候选试验配置恢复为 `RetrievalCandidateExperimentConfig(enabled=False)`，后续会话使用确定性原始查询计划。
3. 保留已经产生的 Harness transcript、transition/event、gate 资格和 artifact 身份，用既有 replay/recovery 机制审查；不得手改 gate decision 或把失败输出标记为可发布。
4. 若需要回退代码，以已验证的仓库版本部署，并保留原始运行版本、来源修订和 artifact 证据。精确 gate/Graph 版本不匹配时应拒绝恢复，不得静默降级。
5. P2 没有启用开关或部署范围；未来只有在补齐真实触发证据和对应验证后才能扩展。

实际检查结果、成对比较结果及本轮部署结论记录在同目录验证证据中。生成日志保存在 `.newsroom/`，不作为新的运行数据库或晋级服务。
