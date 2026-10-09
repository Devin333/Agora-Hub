# 第 4 节：V1 Claim 来源绑定与发布资格

日期：2026-10-09。对应 `tasks.md` 4.1–4.18。本章实现已完成，并在代码冻结后集中测试、根因修复和复验。

## 实现合同

V1 为 `numeric_comparison_result_v1`：两个明确对象在同一真实表格、相同 Dataset/Split 下比较百分比指标。自动提取路径限定 Accuracy 与 baseline；显式 comparison metadata 仍必须与实际表头、行、单位、缩放和条件一致。`%` 与明确 `Unit=percent`、`Scale=percent|fraction` 是确定性单位依据，不能依据 0.92 的大小猜测为 92%。缺列、歧义、NaN/inf、越界、单位或条件冲突不生成合格 Claim。

Claim 绑定稳定 identity/revision、确定性文本、类型化数值、source/cell locator、source/evidence revision/checksum、review binding 及 resolver/gate 版本。数据由已接受 source/document/evidence pack 提供；parser、worker 与 review 仅提供 observation。只有确定性 gate 重放通过后才能获得资格；不合格决策保留审计 checksum，资格引用为 `None`。

| 任务 | 实现位置与边界 |
| --- | --- |
| 4.1–4.5 | `domain/claim_review.py`、`services/claim_evidence_eligibility.py`：V1 类型、严格表格/单位/条件、稳定 ID、locator 与文本 |
| 4.6–4.8 | `services/evidence_builder.py`、`application/claim_evidence_eligibility.py`：opt-in 表格 evidence、真实接受来源/actor scope、observation 候选边界 |
| 4.9–4.10 | `NumericComparisonEligibilityResolver`：四类决策、资格唯一含义、重复去重及冲突拒绝、无有效来源不能被数学自洽替代 |
| 4.11–4.14 | `graphs/claim_evidence_eligibility_gate.py`：精确新 gate，依赖 `ClaimEvidenceGate@1`；重放来源绑定及最终 `claim_models`；资格投影唯一且完整，额外 worker Claim 不得自报 verified |
| 4.15–4.17 | `single_paper_runtime.py`、Graph builders、TaskPlan、memory admission、result committer 和 recovery：受控 Claim 进入最终 `ResearchAnalysis`，精确 opt-in Graph identity 贯穿运行路径 |
| 4.18 | domain/application/resolver/evidence/gate/dynamic 集成正反例；缺证必须真正进入新 gate，受控 HALT 且不发布 |

opt-in 精确 Graph：`research.paper_analysis.dynamic.quality-foundation.graph@1`；资格 gate：`ResearchClaimEvidenceEligibilityGate@1`。既有确定性 `ClaimEvidenceGate@1` 引用验证保留，gate 不接受 worker 自报 passed、route、publication 或记忆/工具权限。

## 动态缺证路径修复

历史 missing-V1 失败发生在 child transcript 提交，未进入资格 gate。静态核查发现 Windows 主机 `LongPathsEnabled=0`，`verified_atomic_create` 把完整目标名重复在临时文件名前缀中，missing-V1 的临时路径约 263 字符，虽最终 bundle 约 249 字符可容纳，staging 路径已超过传统限制。既有 atomic replace 路径已使用短前缀，而 immutable create 尚未对齐。

修复把 immutable create staging 前缀改为固定短值，保留原子 create-if-absent、hard-link publication、fsync、目标/父路径身份核验、link/reparse 拒绝、临时文件清理及并发语义。失败增加有界脱敏 cause chain，不将 OS 错误静默转为成功。回归要求深目录下真实并行 transcript 写入、reopen/verify，以及失败时 cause chain 与无遗留临时文件。missing-V1 保留原 gate、HALT、phase replay、无发布断言，并核对 durable child receipts。

历史日志未保留底层 OS cause，路径长度是静态定位证据；本章集成复验已确认并行 child receipt 可提交、验证及恢复，missing-V1 实际到达新资格 gate，不再以前置 child 错误充当拒绝证据。

## 实际验证结果

- 基础 domain/service/application/gate、结果提交、artifact/transcript store 和固定评测首轮为 262 通过、6 失败、2 跳过。失败根因是 qualified-report fixture 未按 `research_event_tenant_id` 使用 canonical tenant，以及 wrong-cell 反例被严格 projection builder 提前拒绝。修复真实来源夹具，并先构造合法投影再篡改待验 bundle，使负例进入目标 gate；生产校验未放松。
- 同一组复验为 **268 通过、2 跳过，4.63 秒**。两项跳过均为既有 Windows symlink 创建权限不足（`WinError 1314`），没有新增跳过。日志/XML：`.newsroom/quality-foundation/chapter-4-recheck.*`。
- 动态套件首轮为 33 通过、2 失败；修复后 34 通过、1 失败。失败依次揭示 fixture request 缺少真实 `options`、HALT 恢复误查 `RunOutcome`，以及 phase attempt 没有区分 worker retry 与 run replan。使用真实 `AnalyzePaperRequest` 并核对框架实际 lifecycle/decision/budget；未将 NONE 当作 HALT，也未放宽 gate 或发布约束。日志/XML：`chapter-4-dynamic-tests.*`、`chapter-4-dynamic-recheck.*`。
- 最后集中复验动态成功与 missing-V1 为 **2 通过，16.87 秒**，日志/XML：`.newsroom/quality-foundation/chapter-4-final-paths.*`。其余动态 34 项已在上一轮通过。
- missing-V1 明确记录 `ResearchClaimEvidenceEligibilityGate@1` 拒绝及 `claim_eligibility_insufficient_evidence`，三个 child transcript receipt 持久可核验，`publish_artifacts` 未被调用。
- `verify_claims` 的节点 worker retry 默认为零；run 的 `max_replans=1` 允许第二次 dispatch。测试从编译 Graph retry policy 和持久 replan budget 推导总上界，核对无 RETRY、一次 REPLAN、两次连续 dispatch、完整 PLAN/EXECUTE/VERIFY entry/exit、所有预算未越界，最终由 replan 耗尽产生 HALT。
- 恢复核对 `RunLifecycle.HALTED`、公开状态 halted、持久 `HALT_RUN` decision、terminal reason、最新 projection checksum 和 phase history 完全一致。

本章通过不代替第 7 节的全量测试、最终 smoke、固定评测重跑和提交。
