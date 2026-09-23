# Research Agent Input Contract Stage 1

日期：2026-09-23

已提交 Research 领域输入合同的初版模型，代码位于 `backend/research/domain/input_contract.py`。复核发现可信来源、资格消费与深层不可变性尚不完整，任务 2.1 保持待验收。当前实现行为为：

- `ResearchInputCategory` 固定当前任务、论文材料、既有结论、工具观察和必要历史五类；构造器拒绝未知类别，但已知类别仍由调用方传入；
- `ResearchInputItem` 绑定论文、来源 revision、内容引用和状态，`verified_conclusion` 必须带 Research qualification ref，模型候选不能携带资格；
- `ResearchInputProfile` 要求唯一 `current_task`，当前还要求其 content ref 与 objective 相同；该等式尚需修订，不能当作已验收的引用语义。所有 item 检查同一论文并计算 checksum，但嵌套内容尚未深层冻结；
- `PreparedResearchInput` 要求最终 user payload 恰好包含五类，保留 system/tools/output schema/context group refs，并生成 prepared request checksum；
- `ResearchContextProjectionManifest` 只引用已有 Harness context snapshot/archive manifest，不拥有第二套上下文状态；`validate_rollover` 拒绝 pending tool transaction、indeterminate attempt、跨 run/tenant/paper、跳过 window sequence、策略漂移或丢失 current task 保护。

验证：

```text
E:/Anaconda3/python.exe -m pytest tests/backend/research/domain/test_input_contract.py tests/backend/research/domain/test_claim_review.py tests/backend/research/services/test_claim_review_resolver.py -q
18 passed
```

本证据不声称 PromptBuilder、动态 Research caller、持久化 CAS、真实模型评测、窗口 materialization 或 rollout 已完成；对应任务继续保持未勾选。

## 复核边界

任务 2.1 恢复为待验收。当前构造器接受调用方提供的类别及 qualification ref，没有可信来源适配或资格消费验证，不能据此宣称类别由可信来源赋予或 AC-02/AC-03 已覆盖。`PreparedResearchInput` 当前只检查五个字段名；尚未验证实际 provider 请求、条件、授权与预算。嵌套 payload/metadata 仅浅层冻结，checksum 后仍可能发生嵌套修改。`validate_rollover` 是未接线的领域校验，不是 Harness durable activation；同一已提交状态下的合法窗口切换也需要后续修订支持。这些缺口需在实际接线前解决。
