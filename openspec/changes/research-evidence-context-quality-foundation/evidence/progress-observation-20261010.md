# 第 6 节：进度观察与连续性触发边界

日期：2026-10-10。对应 `tasks.md` 6.1–6.5；6.6–6.12 为未触发的条件待办。

## 实现合同

`ResearchProgressObservation` 由 Research domain 拥有。字段包含 run/paper、run 或 claim 范围、存在时的 claim ID/typed resolver status、稳定原因码、有限数值指标、命名 evidence context checksum、字面量 `observation_only=True` 和派生 checksum。

计数 `eligible_claims`、`required_claims` 必须为非负整数，required 必须大于零，eligible 不超过 required；`verified_progress_ratio` 必须为有限数值、落在 0–1 且等于 count 比值。拒绝 bool、字符串、NaN/Inf、额外 metric、错误比例及把分类状态混入指标。指标与上下文采用不可变投影，持久反序列化必须携带并验证 checksum。SHA-256 引用必须为规范小写格式。

resolver 非资格路径生成 claim-scoped observation，绑定 run、paper、claim、binding、resolution、claim contract 及 source/typed observation context。missing-V1 使用同一构造器生成 run-scoped observation，绑定实际 paper_source/document/evidence_pack checksum，不伪造不存在的 Claim 或 resolver status。

gate 输出完整 observation 与 `progress_observation_refs`，复用既有 gate event/trace。资格仍只由真实 resolver 和确定性 gate 决定；进度不参与路由、自动停机、工具/记忆授权或发布。乐观计数不能改变非资格决策，篡改观察会被 replay 拒绝，观察携带控制字段会被 schema 拒绝。

## 实际验证

本章全部实现及测试代码完成后集中运行：

- Claim domain、resolver、source binding、application、qualified report、eligibility gate、固定案例及 CI：**144 通过，2.60 秒**，日志/XML 为 `.newsroom/quality-foundation/chapter-6-tests.*`。
- 动态成功与 missing-V1：**2 通过，15.01 秒**，日志/XML 为 `.newsroom/quality-foundation/chapter-6-dynamic-tests.*`。
- missing-V1 的实际 gate event 包含数值观察、run/paper/source 校验身份，持久事件重放后 observation 与 checksum 相同；既有一次 replan 后 HALT、预算未超限与禁止发布断言仍通过。

本章未改变通用领域反序列化路径，没有新增控制策略、独立 event/state store、context archive 或 window rollover。

## 连续性决定

`context-trigger-audit-20261008.md` 的 119 份本地持久化记录未提供合格触发。6.3–6.5 的要求、核查与“暂不启动”决定已完成；6.6–6.12 保留待办。此次 fake worker/transport 和固定案例不作为生产窗口丢失证据，测试通过也不授权提前实现 P2。