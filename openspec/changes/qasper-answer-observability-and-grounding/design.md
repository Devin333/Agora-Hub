## Context

第二轮 QASPER 300 题的原始汇总受回答模型临时故障和 judge 失败影响。恢复回答失败后的配对结果与原始整轮正确率使用不同分母，必须分开保存；检索覆盖改善也不能直接解释为回答正确率改善。现有 judge 人工校准任务仍未完成，本变更不据此宣布显著收益或 judge 已可信。

现有回答执行路径已经将 `PaperAnswerModelError` 转换为 `AnswerWorkerError`，并在 Harness 中记录 generation failure event/decision metadata。QASPER application service 负责逐题编排和工件，composition 负责 provider，CLI 只调用 application service。恢复能力沿用这些边界。

当前工作区已有错误诊断、进度、judge 和合并的未提交实现。本变更完善并验证这些真实路径，不另建执行器、评分器或状态机。实施必须先完成可观测性与恢复，再修改生成提示词，以免将恢复历史故障与新生成行为混为一次实验。

## Goals / Non-Goals

**Goals:**

- 保留脱敏、限长、可区分故障阶段的原错误信息，以及逐题开始/完成进度。
- 在原样本、原答案和原评分语义上恢复失败 judge；恢复回答系统失败时仅合并显式且合格的失败题。
- 校验恢复输入身份、case 唯一性和父工件哈希，在新的输出目录保留完整样本与逐题来源。
- 在现有回答协议内加强引用、证据不足拒答和 Yes/No 提示；复用确定性结构验证及 Harness 有界控制。
- 用离线测试、既有真实工件核验和项目检查验收，不以新 live 整轮回答运行作为代码完成条件。

**Non-Goals:**

- 不改变检索默认 profile、Qdrant、gold mapping、官方 Answer-F1 投影、既有统计分母或历史结果。
- 不新增复杂 JSON 回答 schema、自动句子语义判定器或以模型自报 `supports` 为依据的门禁。
- 不让 LLM 决定 workflow routing、quality pass/fail、重试预算、memory 写入或发布。
- 不通过反复重答正常题挑选较好答案，不把恢复运行当作独立的同成本完整运行。
- 不在本变更中完成人工 judge 校准，也不因结构检查通过而宣称引用语义支持得到保证。

## Decisions

### 1. 错误经现有 typed-error 链传递，故障阶段与业务结果分开

provider adapter 只提取安全的错误类型、消息、HTTP/provider code、可重试属性和实际存在的失败响应摘要。字段沿 model error -> worker error -> Harness transcript/decision -> QASPER case/report 传递。`retryable` 是诊断信息，不直接触发 LLM 或评测器自行重试；次数和路由仍由现有有界控制决定。

认证、模型不存在或参数错误与服务临时不可用不得仅因同一外层 `model_configuration` 标签而混为一类；原始类型/状态不可得时明确记录未知，不猜造。脱敏在持久化和显示之前完成，限长须记录截断状态，禁止落盘凭据、Authorization header 或无筛选的 provider request。

不采用 CLI 重新猜测异常类别的方案，因为这会丢失 provider 上下文并与 durable transcript 分叉。拒答、证据不足、模型系统失败和 judge 失败继续保持各自语义。

### 2. 进度是编排事件，不是第二条执行路径

application service 在每题开始、完成时通知轻量进度接收器，事件包括 case ID、当前序号、计划题数、阶段和结果状态。CLI 向 stderr 输出并及时 flush，stdout 保持单个机器可读汇总；关闭进度不得改变 case 顺序、答案或指标。进度不能代替逐题工件。

进度输出故障不得伪装成模型配置错误；最终 case 工件保存逐题诊断，工件写入失败必须按运行失败处理，不能发布完整成功报告。本变更不新增中断续跑或逐题 checkpoint 协议。

### 3. 回答恢复是具名父运行上的失败覆盖，不是任意结果拼接

merge service 先解析并校验源报告、answers 和 sample，再应用显式恢复范围。源与恢复结果都禁止重复 case ID，未知题和额外题必须报错。仅源运行中具有目标系统失败状态的题可被回答恢复替换；业务拒答、证据不足、正常答案及单独 judge 失败不能被该模式覆盖。

测量身份先做必填和类型校验，再比较协议、dataset/mapping/cache 身份、原样本及检索/回答配置等影响测量的字段。不能将两边缺少同一字段视为相等。恢复样本应是源样本的具名子集，合并输出仍保留源样本的全部 case 和顺序。旧格式仅允许从已校验工件做明确的版本适配；无法证明的身份不得补成当前默认值。

Git revision/工作区指纹分别保留，不要求只因可观测性修复而机械相等，也不把两个运行伪装成同一个干净提交。若回答 prompt、上下文运行时、模型、检索策略或预算已改变，结果应作为独立比较运行，不能冒充原配置的故障恢复。

输出使用新的目录，记录父 report/answers/sample 的 SHA-256、恢复选择和逐题来源，发布完整 report/answers/sample。失败的恢复尝试如实保留，不按正确与否择优。费用/耗时应保留源运行和新增尝试的归属，不能把只选中答案的 token 总量标为实际总花费。

### 4. judge 恢复冻结回答，只替换 judge 观测

新增独立 application service，只注入 judge 及工件/资产读取依赖；不构造回答模型、检索 session 或 live embedding provider。默认恢复资格为“实际已回答且 judge 失败”；正常 judge、拒答和模型失败均不得进入该恢复模式。只有显式使用 `--include-missing-judge` 时，才补判没有任何 verdict 的已回答题，并将该选择单独记录，不能把缺失观测伪装成已发生的调用失败。

新工件保存可重建的 canonical JudgeRequest 或等价的完整请求快照及 digest，包括问题、参考答案、回答和按原引用顺序渲染的原文。旧工件只有 case/answer/context IDs 时，通过 `QasperAssetService` 验证 dataset、mapping 和 base embedding cache，再用同一 `render_answer_context` 及 valid-marker 索引规则重建。若旧 report 记录 corpus/index 哈希，必须一并校验；若未记录，只能保存当前重建资产的哈希并明确历史字段不可验证，不能声称已证明历史 chunk 内容完全相同。缺 chunk、非法引用或已记录身份不符必须失败，不能以空文本继续。

judge 失败恢复使用同一 judge model/protocol/prompt/schema 身份。新格式严格比较完整配置身份；旧格式若缺 judge model 身份，必须由操作者用 `--legacy-judge-model` 明示历史模型并与当前模型核对。该声明只能建立 model 层面的比较，其余历史 provider、部署与配置仍记录为不可验证，不能冒称完整身份已恢复。更换已知 judge 口径属于独立 rejudge 比较，不与旧判定混合成同口径恢复。恢复前后 answer、context、citations、Answer-F1、coverage 和业务 outcome 不变，只更新 judge 结果、judge 错误、相关派生统计及新增尝试成本。再次失败仍为 judge error，并保留父答案 digest 与尝试来源。

不采用整题重新运行的方案，因为检索、生成和上下文可能改变，无法回答原判定缺失的问题。

### 5. 先明确生成提示，再复用现有可验证结构

保持现有回答文本和拒答协议。提示要求每个被引用的事实由紧邻引用独立支持，按需引用，不能堆积无关引用制造可信度；证据缺失、冲突或无法支持结论时使用既有拒答协议。是否题在足够正向或反向证据下明确输出 Yes/No，“没有提及”不等于 No。问题类型来自请求/题目文本，不读取 gold。

Harness 继续校验现有候选格式、引用 marker 的合法性及其是否指向实际交给模型的 context。新增限制只放在已有、职责合适的确定性 gate 中；若没有可靠的可验证字段，则仅落实提示与测试，不引入通用自然语言断句、推测事实类型或自报支持度门禁。引用有效、引文存在或文本重叠都不是语义蕴含证明。

gate 产生确定性结果/原因，由现有 Harness 按 `max_turns`、`max_replans` 和 retry budget 决定受控修复或停止，所有 phase transition/失败留在 durable transcript。模型候选不得覆盖这些决定。

回答评测的共享 context assembler 使用与输出目录同级的 `<output>.context/events.sqlite3`，通过已有 `SQLiteEventStore`、`EventRuntime` 和 `DurableHarnessEventPort` 保存 context 事件。独立 QASPER context workflow 使用固定版本的 graph/stage 身份，保持 run 隔离；重新打开 event port 后仍可读取原事件。不能用内存 event port 通过架构边界，也不能把进度输出当作持久化历史。

### 6. 验收区分恢复正确性与回答质量改善

恢复验收是可确定验证的：身份合法、资格正确、答案不变、失败如实保留、指标可重算、原工件不变以及没有非预期 provider 调用。错误/进度/恢复使用 fake ports 和真实工件读取测试，覆盖成功和拒绝路径。

生成验收包括实际 prompt 内容、既有引用结构门禁和有界拒绝路径回归。语义质量只能在后续冻结样本、相同口径的配对结果与人工审核/校准后的 judge 中评估。原 300 题汇总、回答失败恢复、judge 恢复和共同有效题比较分别命名，报告各自分母和成本；不能把未校准 judge 的小幅变化标为确定收益。

## Risks / Trade-offs

- [旧工件缺请求正文或身份字段] -> 只使用可验证的版本适配和原渲染规则；无法重建时阻断并给出缺失字段，不伪造 provenance。
- [部分重跑造成择优偏差] -> 恢复范围由源失败状态和显式 case 清单决定，不读取正确性筛选；保留全部尝试与原始汇总。
- [新旧 judge 混用] -> 恢复强校验 judge 身份，口径变更走独立比较。
- [错误消息泄露凭据或工件过大] -> 在统一边界脱敏、限长并记录截断，测试包含凭据样例。
- [强化拒答导致过度拒答] -> 明确 Yes/No 反向证据规则；保持业务 outcome，不用结构 gate 推断证据语义；后续分题型配对审核。
- [引用增多、token 增长而支持率不升] -> 不规定最少多个引用，不扩大上下文预算，保留每次尝试成本和引用数，质量结论另行验证。

## Migration Plan

1. 保存真实新旧运行、恢复范围及配对证据摘要，保留原始故障污染汇总和人工校准 pending 状态。
2. 完成错误诊断/进度与 merge 合同，再实现仅 judge 的独立恢复并进行离线验证。
3. 固定恢复后工件和身份，再修改生成提示与可验证结构回归；禁止将新 prompt 的重答冒充历史故障恢复。
4. 运行范围匹配测试、compile、smoke、architecture 检查和 OpenSpec strict validation，由主控核实 tasks 后提交。
5. 回滚以对应提交为边界；新工件保留为可审计历史，旧工件始终不覆盖。

## Open Questions

- 旧运行可重建哪些 identity/request 字段，需要由恢复 service 的预检结果逐项给出；未知字段不能靠推断放行。
- 生成语义质量的正式验收仍依赖未完成的人工 judge 校准，当前变更只交付协议与可观测性，不替代该工作。
