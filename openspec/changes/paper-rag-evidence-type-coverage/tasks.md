## 1. 根因审计

- [x] 1.1 逐题还原 6 道 `insufficient_evidence` 单篇题：gold 片段都已被检索并接受，均被类型 gate 拦下
- [x] 1.2 统计多角色片段（16/35）和所需类型分布

## 2. 多类型解析（A）

- [x] 2.1 `resolve_types()`、`EvidenceCandidate.evidence_types`、`present_evidence_types()`
- [x] 2.2 内核适配器、覆盖 gate、session gap report、`bounded_document_rag` 改为使用类型并集
- [x] 2.3 `source_verifier` 改用 `dataclasses.replace` 复制候选

## 3. advisory 覆盖策略（B）

- [x] 3.1 `evidence_type_coverage` 策略与校验
- [x] 3.2 主循环和去重穷尽路径接入 `_evidence_type_gap_decision`
- [x] 3.3 `rag_evidence_type_gap_accepted` 事件、指标、replay 决策时间线
- [x] 3.4 `ResearchRAGPolicyBuilder` 默认 `required`，`PaperRAGSession` 使用 `advisory`
- [x] 3.5 评测逐题结果和效率统计记录 `evidence_type_gap_accepted`

## 4. 验证

- [x] 4.1 framework 测试：advisory 放行、required 不变、无生成器时不放行、其他 gate 失败时不放行、多类型满足覆盖、去重穷尽、非法取值、序列化往返
- [x] 4.2 Research 测试：解析器多类型、builder 默认值、论文问答为 advisory、6 道审计题都能进入模型
- [x] 4.3 用 `gpt-5.6-sol` 重跑业务题库，结果见 `evidence/business-qb-type-gate-20261022.md`
- [x] 4.4 `openspec validate paper-rag-evidence-type-coverage --strict`
- [x] 4.5 `python -m scripts.dev smoke`
- [x] 4.6 提交
