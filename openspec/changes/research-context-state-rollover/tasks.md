## 1. 基线与启用契约

- [ ] 1.1 核对单论文 Paper RAG 的真实 caller inventory，记录 source snapshot、transcript、artifact、canonical event、token usage 与恢复端口的接线及缺失能力；确认 V1 只覆盖选定入口。
- [ ] 1.2 冻结任务集、来源快照、模型/工具版本、当前 compaction 基线、关键结论范围规则与质量/成本判定协议；包含短任务、长任务、条件遗漏和预算耗尽案例。
- [ ] 1.3 定义版本化 opt-in policy、有限核验/回读/批量预算、软窗口阈值及必需端口 admission；缺少持久化/预算能力时拒绝启用，记录校准依据。

## 2. 研究工作状态与候选补丁

- [ ] 2.1 复用 Research claim/evidence/lineage 模型实现命题 revision、已知条件、范围未知、核验范围、依赖版本和未决状态契约；保持论文业务字段位于 Research。
- [ ] 2.2 实现业务操作白名单、operation identity、base state / source version 及 producer attempt 检查，禁止模型修改控制面状态；补充过期 attempt 与冲突 identity 测试。
- [ ] 2.3 实现已记录条件删除/放宽/解除关联的确定性准入规则；覆盖小样本条件丢失、未知范围与新证据支持范围修订。
- [ ] 2.4 实现范围区分、争议保留、来源血缘去重和依赖失效；覆盖事实修订后的相关下游失效及无关任务继续。

## 3. 原始历史、提交与恢复

- [ ] 3.1 通过既有端口实现启用路径的独立原始历史 manifest，保留消息、工具输入/结果、来源结构与必要多模态引用；覆盖 checksum、顺序、授权和 retention 不可用语义。
- [ ] 3.2 在活动消息裁剪或 compaction 前加入 archive 准入；覆盖归档失败、legacy 原文缺失和禁止静默破坏性回退。
- [ ] 3.3 实现候选/拟接受状态 artifact → canonical 接受事件 CAS → 当前状态投影的提交链；补充幂等、同 key 不同内容和提交失败场景。
- [ ] 3.4 实现业务投影的 reducer / checkpoint 绑定与恢复，覆盖事件前后崩溃、状态 checksum 不匹配及纯 replay 零 live 调用；复用既有副作用对账。

## 4. 按需回查与成本规则

- [ ] 4.1 实现首次阅读联合抽取及复用本轮证据，覆盖同表多结论；不得默认为每条结论新增读取或独立 verifier。
- [ ] 4.2 实现按当前用途、证据变化、范围扩张、冲突、引用异常和关键范围缺证生成回查决策及原因码；LLM 只返回候选建议。
- [ ] 4.3 实现精确片段读取及表头/表注/实验设置的有界扩展；同证据分组核验且逐条记录支持/反证/不足，达到 batch item/token 上限时拆分。
- [ ] 4.4 实现按命题、条件、用途、来源/依赖、policy/verifier revision 和 actor scope 绑定的复用；覆盖同记录复用、自由改写未确认等价、权限撤销和源删除失效。
- [ ] 4.5 接入总预算下的核验子账本、输入/输出预留与实际结算，记录摘要/回读/失败/子任务成本；覆盖 calls、tokens、reads/bytes、attempts、batch 及货币上限。
- [ ] 4.6 实现无新证据不重复回查、usage 缺失保持 reservation、预算耗尽降级与恢复条件；防止窗口切换或重启重授额度。

## 5. 上下文投影与安全窗口切换

- [ ] 5.1 实现从已提交研究状态生成相关工作视图和交接笔记，绑定 claim/condition/evidence manifest，禁止 notes 或 summary 反向写入正式事实。
- [ ] 5.2 将语义单元接入现有 ContextAssembler / CompactionRuntime，保护命题、条件、核验范围、不确定性及当前判断必需原文，保留已有工具事务检查。
- [ ] 5.3 在最终 provider materialization 验证条件关联和 prepared request fingerprint，实际计入 system/tool schema、封装开销及输出预留；覆盖状态完整但请求漏条件、保护内容自身超限。
- [ ] 5.4 实现安全点 rollover 与 durable activation，沿用 run/conversation、policy、预算、attempt 和操作 identity；pending/indeterminate tool outcome 先完成或对账，不通过新窗口重复执行。
- [ ] 5.5 覆盖不同安全点及连续多次切换的确定性一致性、有效记录额外语义核验数为零、切换失败不激活，以及回滚后固定策略恢复。

## 6. 单论文真实接入与可见结果

- [ ] 6.1 在 Research application service 和 composition 接入真实检索、补证、回答与恢复路径，通过端口注入业务核验规则；不以生产 fake 或跨层访问替代。
- [ ] 6.2 接入最终关键结论覆盖 gate，复用已有有效检查、集中处理缺口；输出条件、争议、已查范围及类型化未完成信息，防止 partial result 被标为完整成功。
- [ ] 6.3 在现有诊断/结果中呈现 state/window version、回查原因、复用/失效、成本及未决 reservation，不引入独立 UI；确认权限过滤和引用授权。
- [ ] 6.4 完成真实入口集成用例及 PRD AC-01 至 AC-19 覆盖映射，分别验证确定性 gate 与模型语义能力边界。

## 7. 评估、发布与文档收口

- [ ] 7.1 在固定记录上比较当前 compaction、旧工具结果移出窗口实验基线和本策略；统计所有调用及短任务附加开销，不修改生产 gate 获取有利结果。
- [ ] 7.2 完成真实模型配对评测与盲审，报告条件遗漏、无依据泛化、引用、完成率、token/cache/费用/延迟及误差；满足 PRD AC-20 与设计 D10 的可比质量/效率门槛后才允许扩大灰度。
- [ ] 7.3 验证 shadow → 新 run opt-in 的启用与回滚；正确性失败阻断新 admission，已有 run 固定 policy 安全完成或停在可恢复 checkpoint，保留历史和未决写入。
- [ ] 7.4 运行 `.venv\Scripts\python.exe -m scripts.dev compile`、范围匹配测试、`.venv\Scripts\python.exe -m scripts.dev smoke` 和 `openspec validate research-context-state-rollover --strict`；修复失败根因并记录通过/阻断与部署验证边界。
- [ ] 7.5 更新实现事实、指标报告、配置与运行说明，检查需求/验收映射，按本变更范围提交代码；所有任务验收后再评估归档，不把文档 apply-ready 当作实现完成。
