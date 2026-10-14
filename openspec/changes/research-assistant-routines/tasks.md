## 1. 范围与前置能力核对

- [ ] 1.1 确认本变更是桌面首版之后 default-off 的论文例行任务能力；冻结首期 `paper_watch`、本机存活触发、每日/每周计划及应用内收件箱的范围与非目标。
- [ ] 1.2 逐项核对既有 owner/workspace/conversation、Research Run、memory、Wait/approval、durable event、scheduler/queue、publication 与 local desktop ports；记录可复用实现、缺口及前置 change 的真实验收责任。
- [ ] 1.3 核定本机事务 store、durable queue、Harness invocation 查询/幂等和 fencing 的适配方案，覆盖现有 JSON/Redis 默认组合的不足；禁止第二套运行控制器或隐式新增外部服务。
- [ ] 1.4 固定并记录可见的授权有效期、catchup/lookback 上限、论文数、调用/token/费用预算与周期时区默认值，以及 capabilities 阻断行为；未批准真实 provider/预算前不做 live 调用。

## 2. 助手身份与授权用例

- [ ] 2.1 在 Research-owned domain/application/ports 中实现 owner/workspace 范围内唯一的 `AssistantProfile` 与已有 Conversation、资料库、Run、memory 的引用关系，添加重启和跨 owner 测试。
- [ ] 2.2 实现默认不执行的 Routine 候选、不可变 revision、服务端规范化摘要和确认合同；拒绝未支持任务、客户端自报身份、未知工具和明文 secrets。
- [ ] 2.3 实现有期限的 `RoutineGrant` 与可信后台 delegation，绑定 scope、provider/data policy、budget 和 authorization/restore epoch；覆盖退出登录、切换 owner 与重新登录不得自动恢复。
- [ ] 2.4 实现执行字段编辑后的 `NEEDS_CONFIRMATION`、旧授权失效/取消、新版显式确认及非执行展示字段编辑；测试旧队列和旧审批不能运行新版或恢复旧版。
- [ ] 2.5 实现暂停、恢复、撤销、归档与复制草稿用例，明确在途 Run 的继续/取消差异及生效确认；为撤销竞态和授权过期增加测试。
- [ ] 2.6 实现 one-shot grant 原子绑定唯一 occurrence 的立即运行和 request 幂等，不隐式启用计划；验证跨请求/版本重用拒绝、归档/撤销状态限制、重复提交与活动 Run 返回行为。

## 3. 本机持久化与预算

- [ ] 3.1 在既有受控数据根实现 schema-versioned 助手、Routine/grant/occurrence 存储适配及唯一性约束，不复制 Run transcript、论文库或 artifact 内容。
- [ ] 3.2 实现 occurrence 与 outbox 的原子提交、durable queue/inbox 幂等接线和稳定 event ID；测试提交前后崩溃、投递失败、重投及多进程并发领取。
- [ ] 3.3 接入 durable lease/fence 与失效检查，保持一 Routine 一活动 Run（包括 WAITING），持久化合并/跳过的触发记录。
- [ ] 3.4 实现 Routine/owner 周期共享预算的原子预留、消费、结算和未知费用保留；验证并发超额、retry/replan、补跑、revision 更新及重启不能重置余额。
- [ ] 3.5 将新状态纳入 canonical redacted events 和现有存储清单；验证 secrets/私人上下文脱敏、磁盘失败和权限错误不被报告为成功。

## 4. 调度与本机生命周期

- [ ] 4.1 将每日/每周结构化计划接入既有 scheduler，保存显式 IANA timezone、本地 slot 和 UTC due time；必要的中立能力在 framework 原归属层补齐并覆盖现有 schedule 回归。
- [ ] 4.2 实现并测试 DST 缺失时刻跳过、重复时刻仅第一次、系统时区变化、时钟倒退及重复 tick 不重复运行。
- [ ] 4.3 实现有限 `RUN_ONCE`/`SKIP`、catchup age、积压区间合并与有界计算；测试离线三天、超期遗漏、主动暂停、新 revision 与恢复旧备份的不同规则。
- [ ] 4.4 将 Routine 调度接入既有 local runtime lifecycle，不新增 daemon/开机自启或云端执行者；验证服务关闭、休眠恢复及能力投影。
- [ ] 4.5 实现活动 Run 期间至多一个合并待执行 occurrence，重新准入前检查时效/授权/预算；测试等待审批时的积压和取消终止未知时的并发阻断。

## 5. Harness 准入、恢复与取消

- [ ] 5.1 通过 application service 与既有 Harness 提交端口绑定稳定 invocation key/Run；覆盖提交已接受但响应丢失的查询、重投和 checkpoint 恢复。
- [ ] 5.2 固定精确 Graph/gate/tool/model policy 版本及有限 turns/replans/retries/time/calls；缺失 provider、gate、durable store 或 capability 时 fail closed，首期保持静态 Research 路径。
- [ ] 5.3 在 source/model/tool、memory context、publication 和内容提醒的 authority boundary 校验当前 grant、owner、scope、expiry、epoch、fence 及预算，测试撤销与动作准入竞争。
- [ ] 5.4 复用 Harness Wait/approval 与取消控制，区分取消已请求、终止确认和 indeterminate；测试迟到审批、重复信号、失效 lease 及不能确认远端终止。
- [ ] 5.5 接入 source/model/tool 的副作用 receipt/reconciliation，结果未知且不可证明幂等的远端调用不得盲目重试；测试收费模型响应丢失、崩溃后的原身份恢复和预算结算。
- [ ] 5.6 将 phase transition、Routine 事件和运行投影连接到 canonical durable event/replay；验证重复 replay 的 source/model/tool、新 Run、publication 和通知调用数均为零。

## 6. 真实论文追踪闭环

- [ ] 6.1 使用现有真实 Research source ports 实现受主题、来源、窗口、条数和预算约束的收集；测试 metadata/abstract/full-text 的真实材料深度，禁止生产 fixture fallback。
- [ ] 6.2 复用 arXiv/DOI/规范 URL 身份和 workspace catalog，完成跨来源、跨运行去重及论文版本保留；测试同名不同论文不误合并。
- [ ] 6.3 实现逐源成功游标、覆盖窗口、分页/截断与失败记录；验证失败不推进水位，超出 lookback 的缺口明确可见且无无界回补。
- [ ] 6.4 通过现有 Research worker 生成候选推荐与报告，并绑定确定性证据/引用/分析深度 gate；覆盖伪造引用、缺失 gate、质量失败与有界修复。
- [ ] 6.5 通过既有 controller publication 幂等发布报告和资料关联，并以 receipt 更新已交付标记；测试发布后确认前崩溃、对账、重投不重复报告或提醒。
- [ ] 6.6 实现 `new_results`、`no_change`、`partial_failure`、`failed`、`needs_action` 及取消/未知状态的诚实投影，覆盖全部源失败、部分成功、模型/VERIFY 失败不得静默、预算耗尽和正常无合格新增。
- [ ] 6.7 通过已有 personal memory 服务实现默认关闭的获准召回和远程处理策略、实际 ID/revision 记录以及反馈候选确认；测试停用/删除、不可信论文指令、外发限制和不得修改 active skill。

## 7. 工作台与结果收件箱

- [ ] 7.1 通过应用服务/API 接入既有论文 App 的助手设置、Routine 草稿/确认/列表及单次运行，不另建通用控制台；覆盖 owner 与 workspace 身份透传和授权拒绝。
- [ ] 7.2 实现真实 Run 详情、时区/下次执行/runtime 能力状态及暂停/取消/撤销差异；测试服务离线或持久化失败时不能乐观显示已停止/已保存。
- [ ] 7.3 实现基于稳定 outcome-key 的应用内结果与健康收件箱，覆盖重复投递、已读状态、相同故障合并、恢复事件及 `no_change` 静默。
- [ ] 7.4 接入既有论文/报告/证据阅读深链接与最小信息展示；验证跨 owner 拒绝、撤销后不新增推荐以及无系统推送/邮件/webhook 调用。
- [ ] 7.5 增加 UI 组件与端到端测试，覆盖空状态、确认费用/数据流、立即运行不启用计划、恢复授权、取消未知与真实结果导航。

## 8. 迁移、备份与回退

- [ ] 8.1 实现 default-off 的受控 schema 迁移与 capability flag；旧安装/资料升级不创建活动 Routine，手动 Research 与阅读路径保持可用。
- [ ] 8.2 扩充现有备份/导出清单，校验 owner/checksum/schema，排除 credentials/delegation token；测试磁盘失败与跨 owner 导入。
- [ ] 8.3 实现隔离恢复后的 epoch 提升、Routine 停用、旧消息/审批/lease/提醒失效和预算待对账；覆盖恢复到撤销前快照也不能自动执行，普通重新确认不能重置消费或替代独立追加预算授权。
- [ ] 8.4 实现功能关闭停止准入、受控取消、fencing 和只读历史保留；演练重启后不复活以及不兼容版本的写入阻断/隔离恢复，不破坏性逆迁移。

## 9. 验证与渐进启用

- [ ] 9.1 对五个 capability 的全部场景建立测试/证据映射，运行受影响后端、接口、存储、scheduler、Harness、前端和 architecture 检查；确认 interface/service 与 Research 依赖边界无回退。
- [ ] 9.2 完成端到端故障注入矩阵：重复触发/投递、崩溃窗口、旧 worker、撤销/审批竞争、预算并发、DST/离线、部分来源失败、publication 对账及只读 replay。
- [ ] 9.3 在相同候选版本运行 `python -m scripts.dev compile`、`python -m scripts.dev test`、必需 pre-commit gate `python -m scripts.dev smoke` 与适配前端检查；根因修复失败，不跳过测试或弱化断言。
- [ ] 9.4 运行 `openspec validate research-assistant-routines --strict`，核对前置能力资格和版本绑定；不代勾其他 active change 的未完成任务。
- [ ] 9.5 仅在用户明确批准 provider、数据范围与预算后，完成真实论文发现、引用报告、无更新静默、重启/取消和逐项启用的 live 验收；未获授权则保留未完成，不能以 fixture 结果替代。
- [ ] 9.6 记录真实输入/版本、gate 与产物、费用/隐私、备份恢复、disable/rollback 和剩余风险证据；全部必需 gate 通过后方可逐个显式启用 Routine，不将文档完成等同产品发布。
