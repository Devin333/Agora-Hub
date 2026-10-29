## Why

第二轮 300 题运行的端到端正确率为 130/278（0.468），受到临时网关故障和 judge 失败影响。恢复 26 道 `model_configuration` 失败中的 25 道后，251 道共同有效题的正确数为 131→139，检索改善仅小幅转化为回答收益；引用支持、无解题拒答和过度拒答仍是生成端短板。

当前失败工件缺少错误类型和消息，judge 无法独立恢复，整轮重跑会改变答案并破坏对照。因此先补齐可追溯、可恢复的评测，再改引用和证据判断约束。

## What Changes

- 保存脱敏、限长的 provider 原错误类型、消息、状态、retryable 信息和协议失败输出摘要，区分真正配置错误与临时网关响应错误。
- CLI 将逐题开始与完成进度输出到 stderr，stdout 保持机器可读汇总；工件保存逐题错误和恢复来源。
- 新增 judge 独立恢复：复用冻结回答和引用原文，仅重跑失败 judge，输出独立完整工件并记录父工件哈希，不重新回答或检索。
- 完善系统失败题的显式恢复及合并，验证测量身份、case 唯一性、失败资格和原样本顺序，禁止覆盖正常题。
- 强化生成端提示词：逐条引用紧邻并独立支持所述事实；证据不足必须按协议拒答；是否题根据正向或反向证据给出 Yes/No，不能把未提及当作 No。
- 保留 Harness 的确定性验证和有界重试权；仅加强可验证的引用结构，不把结构通过或模型自报当作语义支持证明。
- 记录实际新旧 300 题工件及配对分析，保留未校准 judge 的限制和故障污染的原始汇总。

## Capabilities

### New Capabilities

- `qasper-answer-recovery-observability`：错误诊断、逐题进度、冻结回答的 judge 恢复与不可变合并。
- `paper-answer-evidence-discipline`：生成端证据不足、引用和 Yes/No 的明确约束及受控结构验证。

### Modified Capabilities

无。保留既有回答 outcome、Harness 状态机和原评分分母定义。

## Impact

- 涉及 paper answer model/worker 的错误传递，QASPER answer case/judge/application/composition/CLI，以及 answer generator 的提示和协议元数据。
- 延续工作区中上一模型已开始但未提交的可观测性实现，补齐真实行为测试与架构检查。
- 不修改历史工件，不引入生产索引或 memory 写入；不为验证代码启动完整 live 回答评测。
