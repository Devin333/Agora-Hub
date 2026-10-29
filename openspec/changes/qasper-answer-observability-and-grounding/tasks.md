## 1. 证据与恢复合同

- [x] 1.1 核验并记录新旧 300 题、26 题回答故障恢复和共同有效题比较的源工件、分母与限制，保持 judge 校准待办。
- [x] 1.2 定义恢复模式、必填测量身份、case 资格、父工件哈希和逐题来源，明确旧工件适配与阻断规则。

## 2. 错误诊断与进度

- [x] 2.1 将脱敏限长的 provider 错误类型、消息、状态、retryable 和失败输出摘要沿 model/worker/Harness 传入 case/report。
- [x] 2.2 增加逐题开始/完成进度，CLI 写入 stderr 并保留 stdout 机器可读汇总。
- [x] 2.3 测试临时故障与真实配置错误、脱敏/截断、业务拒答保持和进度顺序，验证不会创建额外模型调用。

## 3. 回答失败恢复与合并

- [x] 3.1 严格校验 required identity、case 唯一性/子集与源系统失败资格，禁止正常题覆盖和未知 ID。
- [x] 3.2 在新目录生成完整 report/answers/sample，保留原样本顺序、父哈希、各次代码身份及逐题尝试来源，重算派生指标。
- [x] 3.3 测试缺失身份、配置漂移、重复/多余题、正常题替换、再次失败和原工件不变；区分结果统计与新增尝试成本。

## 4. judge 独立恢复

- [x] 4.1 为新回答工件保存可重建 judge 请求及身份，按已校验资产和原渲染规则支持旧工件，缺失时明确阻断。
- [x] 4.2 实现只处理实际已回答且 judge 失败子集的 application service/composition/CLI，不构造回答、检索或 embedding provider。
- [x] 4.3 生成带父工件哈希和逐题来源的完整恢复输出，仅更新 judge 观测及对应统计，保留再次失败。
- [x] 4.4 用 fake judge 测试新/旧请求重建、judge 身份不匹配、非法引用、拒答/系统失败排除、答案与 coverage 不变及调用次数。

## 5. 生成端证据纪律

- [x] 5.1 在现有回答协议中强化紧邻且独立支持的引用、证据不足拒答、Yes/No 正反证据规则，保留 prompt/协议身份。
- [x] 5.2 复用现有引用 marker/context 合法性 gate 及 Harness 有界处理；不新增语义自报门禁或复杂回答 schema。
- [x] 5.3 测试实际 prompt、合法/非法引用、明确不足、Yes/No 提示和 gate 失败的受控路径，保持现有业务 outcome 与预算。

## 6. 集成与验收

- [x] 6.1 运行范围匹配的 QASPER/answer/Harness 测试与 architecture 检查，核验 CLI 分层和恢复零非预期 provider 调用。
- [x] 6.2 运行 `python -m scripts.dev compile`、`python -m scripts.dev smoke` 和 `openspec validate qasper-answer-observability-and-grounding --strict`，修复失败根因。
- [x] 6.3 核对工件不变性、分母、恢复谱系和质量限制，更新证据与任务状态，由主控提交完成代码变更。
