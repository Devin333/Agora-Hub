# Research Agent Input Contract Stage 1

日期：2026-09-23

已完成的首片是 Research 领域的确定性输入合同，代码位于 `backend/research/domain/input_contract.py`：

- `ResearchInputCategory` 固定当前任务、论文材料、既有结论、工具观察和必要历史五类；类别由受控构造器赋予，未知类别不会被接受；
- `ResearchInputItem` 绑定论文、来源 revision、内容引用和状态，`verified_conclusion` 必须带 Research qualification ref，模型候选不能携带资格；
- `ResearchInputProfile` 要求唯一 `current_task`，并要求其 content ref 与 objective 相同；所有 item 必须属于同一论文并具有稳定 checksum；
- `PreparedResearchInput` 要求最终 user payload 恰好包含五类，保留 system/tools/output schema/context group refs，并生成 prepared request checksum；
- `ResearchContextProjectionManifest` 只引用已有 Harness context snapshot/archive manifest，不拥有第二套上下文状态；`validate_rollover` 拒绝 pending tool transaction、indeterminate attempt、跨 run/tenant/paper、跳过 window sequence、策略漂移或丢失 current task 保护。

验证：

```text
E:/Anaconda3/python.exe -m pytest tests/backend/research/domain/test_input_contract.py tests/backend/research/domain/test_claim_review.py tests/backend/research/services/test_claim_review_resolver.py -q
18 passed
```

本证据不声称 PromptBuilder、动态 Research caller、持久化 CAS、真实模型评测、窗口 materialization 或 rollout 已完成；对应任务继续保持未勾选。
