## Context

这是 `paper-agent-desktop-app` 之后的独立 opt-in 产品能力，不是该首版范围的扩大，也不是后台任务已可发布的证明。本轮只新增 OpenSpec artifacts，不修改生产代码、创建真实 Routine、启用 feature flag、调用收费 provider 或改动其他 active change。

已核对的复用入口：

| 入口 | 本变更使用方式与限制 |
| --- | --- |
| `framework/harness`、`backend/research` | 保留 Graph 外层编排、有界 AgentLoop、确定性 VERIFY 和 controller-owned publication；不建立聊天驱动的第二套 runtime。 |
| `interfaces/services/schedule_service.py`、`framework/workers/scheduler` | 复用时间计算与调度合同；当前 service 默认组合 `LocalJsonScheduleStore` 和 Redis queue，enqueue 与 schedule 状态更新分离，不把它直接认定为本机事务调度方案。 |
| `interfaces/services/personal_memory_service.py` | 复用 owner 隔离、confirmed/candidate 区分、实际召回记录和停用/删除控制。 |
| `interfaces/services/harness_wait_service.py`、`framework/events` | 复用审批、取消、durable event 和只读 replay，不另写以 UI 状态为权威的任务恢复逻辑。 |
| `paper-agent-desktop-app`、`research-local-desktop-readiness` | 复用身份、配置、数据路径、桌面生命周期、备份与隔离恢复；这些变更的未完成验收仍归原 owner。 |

首期是 Windows 单机个人 Research Assistant：在用户明确启动并授权的既有本机 runtime 存活时工作。关闭 runtime、休眠或关机期间不运行；不新增开机自启、系统服务、云端代跑或独立守护进程。关闭窗口是否终止 runtime 服从已有桌面生命周期，界面必须显示实际状态，不能承诺全天在线。

## Goals / Non-Goals

**Goals:**

- 用持久身份、上下文和受控 Routine 实现长期助手，而非保持一个无限 LLM 进程。
- 交付“授权定时追踪一个论文主题 → 真实收集 → 去重 → 候选推荐 → gate → 本地结果”的完整闭环。
- 让授权、收费、远程数据处理、暂停/撤销、失败、通知和恢复均可解释、可测试。
- 在重投、崩溃、重启、失去租约和旧备份恢复时避免重复业务效果与越权动作。

**Non-Goals:**

- 不增加通用 Agent 平台、任意角色市场、任意脚本/工具模板、电脑操作、邮件或公开发布。
- 不重写 Harness、scheduler、记忆系统或论文资料库，不允许 Research 依赖 interfaces、infrastructure 或 legacy paper_radar。
- 首期不做外部 webhook 触发、跨设备/云同步、原生系统推送、多 Agent 动态 fan-out 或 skill 自动演进。
- 不假定存在未验收的本机 queue、durable event store、provider 或执行隔离能力，不以 fake 路径作为生产降级。

## Decisions

### D1. 业务身份与执行状态分离，复用已有资料归属

长期身份使用 `AssistantProfile`，首期每个 owner/workspace 一个 Research Assistant。Conversation、资料库和 Run 使用已有稳定 ID；只增加关联，不复制第二份事实源。可选 Conversation 是交互入口，不是 Routine 的后台调用凭据，也不能以模型回复决定任务完成。

| 对象 | 核心合同 |
| --- | --- |
| `AssistantProfile` | `assistant_id`、owner/workspace、职责与 policy revision；不内嵌 provider 密钥。 |
| `Routine` / `RoutineRevision` | 稳定 ID、生命周期和不可变执行定义：任务种类、主题/源、触发、工具/provider、数据/记忆范围、预算与通知规则。 |
| `RoutineGrant` | 对精确 revision/digest 的用户授权，包含 owner/workspace、有效期、权限、远程目的地/数据类别、预算和授权代际。 |
| `RoutineOccurrence` | 一次逻辑触发及其 `run_id`、due/observed 时间、授权版本、幂等键、lease/fence、投递和终态关联。 |
| `RoutineInboxItem` | 可读结果与提醒状态，引用既有 run/artifact/evidence；不是另一份报告或执行状态。 |

业务模型/用例/ports 归 `backend/research`，API/桌面通过 application service 调用，具体存储与 queue 适配归 `infrastructure`。framework 只接收中立的调度、任务、授权和执行合同，不认识论文推荐业务。

替代方案：把每个聊天线程当永久 Agent，或新建通用 Agent 管理器。两者会混淆身份、运行、权限和恢复，故不采用。

### D2. 显式、版本化、可撤销的持续授权

LLM 可以生成 Routine 候选，只有确定性应用服务能验证并保存；候选和新建 Routine 均不运行。确认页显示时间/时区、下次执行、收费与数据流、来源、最大工作量、有效期、失败/离线规则。Routine payload 只保存凭据引用，不保存登录 token、API key 或原始秘密。

执行权限是 `RoutineGrant` 与当前 owner/workspace 权限、工具 policy、feature/capability gate 的交集。后台执行使用现有可信后端验证的 owner-scoped delegation，不复用前端 cookie，不允许客户端自报 owner 或 grant 摘要即获得权限。用户主动退出登录、owner 切换或恢复代际变化使后台 delegation 失效；再次登录不自动恢复执行，必须明确恢复并按需要重授权。

建议生命周期：`DRAFT -> ACTIVE <-> PAUSED`，以及 `NEEDS_CONFIRMATION`、`REVOKED`、`ARCHIVED`。这些是产品状态，不能替代 Harness Run 的执行状态。授权过期映射为 `NEEDS_CONFIRMATION` 并保留原因；恢复已撤销任务需要新 grant；归档为不可再调度的历史记录，复制时生成新草稿。

- 修改主题、来源、触发、provider、工具、记忆/数据范围、预算、有效期或提醒内容策略：生成新 revision，旧执行授权失效，停止新准入，对旧版活动 Run 发出受控取消；用户确认新版前不能继续。仅显示名称变更不扩大授权。
- 暂停：阻止新准入及尚未入场的队列项；已入场 Run 仍可在原有效授权内完成。界面明确提示，可另行取消当前 Run。恢复重新校验授权，暂停期间不补跑。
- 撤销/归档：先持久化失效及 fencing，再请求取消；阻止后续外部调用和新发布，但不声称能撤回已经发出的网络请求或既有产物。
- “立即运行”：必须持有覆盖本次行为的有效 grant。`DRAFT`/`PAUSED` 的独立确认生成绑定 owner/workspace、精确 revision/digest 和 request ID 的 one-shot grant，原子绑定唯一 occurrence；同一 grant 只允许该 occurrence 的有界执行，重投返回原身份且不启用计划。`NEEDS_CONFIRMATION`/`REVOKED` 必须先获得新授权，`ARCHIVED` 只能复制成新草稿。已有活动 Run 时返回该运行，不绕过并发上限。

替代方案：一句自然语言永久授权或编辑后沿用旧 grant，无法对齐真实范围与费用，故不采用。

### D3. 有界的本机时间与 missed-trigger 语义

首期支持每日、每周的结构化日历触发及手动单次执行，编译为既有 scheduler 支持的 schedule；不开放任意 cron/脚本。时区为确认时保存的显式 IANA 值，界面从用户设置预填并要求可见确认，不能继承 scheduler 的隐式默认值或跟随系统时区静默改变。

`scheduled_for` 使用 UTC，另存本地逻辑 slot 与原时区。DST 不存在的时刻跳过并记录原因；重复时刻只取第一次对应的 UTC instant，slot 不因 fold 产生两次运行。时钟倒退或重复 tick 不改变既有 occurrence 身份。

默认 missed policy 为 `RUN_ONCE`：在有限、已显示的 `max_catchup_age` 内最多合并到最近一次应触发 slot；更旧区间记为跳过。可选 `SKIP`；首期不支持无界逐次追赶。处理停机积压必须使用有界计算，不按离线天数无界遍历。主动暂停、授权失效、恢复旧备份或新 revision 的启用不是停机补跑，默认从确认后的下个 slot 开始。

每个 Routine 最多一个活动 Run，`WAITING` 也占用名额；执行期间新 slot 最多保留一个有界合并的待执行 occurrence，入场前重验时效/授权/预算。现有通用 scheduler 缺少需要的中立能力时，在其本层补齐并测试，不能复制一套 Research cron 引擎或改变其他任务默认行为。

替代方案：桌面关闭仍宣称在线，或醒来后执行所有遗漏任务，会导致误导和费用突增，故不采用。

### D4. 持久化 occurrence 与单一运行权威

```text
桌面工作台 / 对话候选
    -> Routine application service（确认、owner、revision、grant）
    -> 既有本机 runtime 中的 scheduler adapter
    -> occurrence + outbox（同一事务）
    -> durable queue / inbox dedup
    -> Harness admission（授权、budget、lease/fence、policy）
    -> Research Graph: collect -> evidence -> analysis -> report -> VERIFY
    -> controller-owned artifacts/storage
    -> canonical events -> run projection / result inbox
```

日历触发幂等键由 owner/workspace、Routine ID、不可变 revision、恢复代际和逻辑 slot 组成；手动触发使用确认后的 request ID。去重约束必须在持久化 store 原子执行，不是 Python 进程内集合。业务原子更新与 outbox 写入共用事务；跨 store 的 canonical event 与 queue 通过稳定事件 ID、inbox 和重投收敛，不假称跨库原子事务。

本机 adapter 复用既有受控本地数据库与数据根，明确 Routine 表、grant、occurrence、outbox/inbox、租约、预算和收件箱的备份清单；不另外存 Run transcript 或 artifact 内容。实施先核对可复用 port/store 的实际能力，补齐本机事务适配后才启用，不默默拉起 Redis 或退化为内存 queue。

提交 Harness 使用稳定 invocation key，崩溃后先查询/重投同一身份，不生成第二个 Run。当前租约/fencing 控制入场、续租和后续副作用。所有 Harness phase transition 写入 canonical durable transcript；Routine 的生命周期与入场事实通过同一事件合同记录，UI 只是投影。

系统承诺逻辑触发和本地业务效果幂等，不承诺任意远端调用 exactly-once。所有可能收费或产生不可逆效果的 source/model/tool 请求，发送后结果未知时均进入既有 `indeterminate`/reconciliation 路径，通过稳定请求身份、provider 幂等保证或 receipt 对账确认结果。无法排除原请求已经执行时，不因超时、重启或租约失效自动重发，也不释放费用预留；必要时投影为 `needs_action`。Replay 只读事件和已记录结果，不能调用 source/model/tool、创建 Run 或发送提醒。

替代方案：直接包装当前 `tick()` 并在 enqueue 后写 JSON，无法覆盖崩溃窗口；另写永久 Agent 循环则绕过 Harness，均不采用。

### D5. 预算与权限在每次副作用前重新收紧

每个 Run 固定绑定 revision、grant、Graph/gate/skill/tool/model policy 版本以及有限的 `max_turns`、`max_replans`、retry budget、运行时长、论文数量、source/tool calls、token 与费用上限。入场原子预留 Routine 及 owner 共享额度，重试/子任务共用而不是重置预算；预算周期和时区随授权明确，不因客户端时钟或重试滚动重置。

在 source/model/tool 调用、memory context 构建和 publication 前验证当前 grant、owner、epoch、fence 与剩余额度。无法给模型收费建立保守预留、无法获得所选 provider 或必要 gate/store 能力时保持阻断，不切换到未经授权的模型、来源、凭据或工具。恢复时只能继续原绑定或进入受控 halt，不能按最新配置静默重新执行。

首期使用现有静态 Research Graph；动态子 Agent 不是前置需求，若后续启用须先通过原并行变更的 release gates。暂停/取消功能复用既有 Harness authority，不由聊天内容直接操作执行器。

替代方案：仅在 Routine 创建时查预算/权限，不能抵御排队后撤销和并发消费，故不采用。

### D6. 论文追踪保持真实证据和诚实结果

首期只允许受支持的 `paper_watch` 任务种类。数据源、时间窗、筛选、最大论文数与保存范围在确认时可见；生产路径使用真实 source port，测试可以注入 fixture。论文标识按已有 arXiv/DOI/规范 URL 规则解析和去重，复用 workspace catalog，不用标题相似作为唯一身份依据，也不把标题摘要包装成已完成全文阅读。

每个来源保留成功处理游标和覆盖窗口；source 失败、分页截断或解析失败不得推进为成功水位。离线恢复的抓取仍受授权 lookback/条数/调用预算限制，无法覆盖的区间明确标为缺口，不能以“无更新”掩盖未检查。

模型只提供相关性、推荐理由和候选报告。Research 的确定性 gate 校验来源、证据引用、结构、真实分析深度、规则阈值和已知失败；它不把格式正确等同于语义真理。候选不满足 gate 时执行有界 retry/replan/halt，不能发布为成功推荐。报告、论文关联与新的“已交付”标记只在 controller-authorized publication 后幂等提交，崩溃后从已记录 receipt 对账。

产品结果投影至少区分 `new_results`、`no_change`、`partial_failure`、`failed`、`needs_action`；取消仍显示既有 Run 的 `cancelled` 状态。只有必需来源窗口完整检查、必要筛选/分析/VERIFY 正常完成且无新增可交付结果时，才允许 `no_change`。正常业务筛选淘汰必须与执行或验证失败区分；模型失败、gate 重试耗尽、预算中止及未决调用不得映射为 `no_change`。部分成功可以呈现已通过 gate 的结果，但必须显示遗漏来源和覆盖范围，不把整次 Run 标为全量成功。

个人记忆默认不使用/不外发；只有在用户明确允许的 confirmed memory 范围内召回，记录实际 ID/revision。每次使用重新检查删除/停用状态；普通反馈可以存为反馈事实，但变成活动个人偏好需要用户确认，不能借 Routine 自动写 memory 或 active skill。

替代方案：直接把搜索结果或 LLM 推荐文本当最终报告，无法满足现有 evidence/quality/publication 约束，故不采用。

### D7. 低噪声工作台、恢复安全与分阶段发布

在既有论文 App 中接入助手设置、例行任务、执行详情和结果收件箱，不替换阅读布局。执行状态来自 application service 对 durable records 的投影；聊天中的“完成”不改变真实状态。操作区区分暂停未来触发、取消当前 Run、撤销持续授权和一次性立即运行。

首期仅应用内收件箱，不向系统推送、邮件、外部 IM 或 webhook 发送消息。新合格论文、需要用户决定以及重要失败创建稳定 outcome-key 去重的提醒；相同未解决错误不每次刷屏，恢复成功可更新原事件。每次运行仍可在历史查到，`no_change` 不新增提醒。提醒默认展示最少信息，详细论文/记忆内容只在 owner 已认证的详情页展示。

备份/导出不携带 credentials/delegation token。旧备份恢复遵守既有隔离根、owner、checksum 和受控激活规则；提升 restore/authorization epoch，使所有还原的 Routine 停用并要求重新确认，旧 outbox、lease、队列消息、预算快照和提醒不能自动生效。导入的旧历史只读，不成为新任务的触发源。恢复及授权代际变化不重置本周期已消费额度或未决费用责任，旧快照不能作为当前可用余额的充分依据；无法可靠重建时，收费准入保持阻断，直到完成可信对账或用户明确批准独立追加预算。普通 Routine 重新确认不构成预算重置或追加收费授权。

发布 gate 默认关闭；只有实际 owner、local store/queue、durable events、取消/恢复、source/model 和 gate 绑定的受测 capability 齐备后，用户才能逐个启用。关闭功能停止新准入、撤销该功能的后续副作用许可并受控取消活动运行；保留记录、已发布产物与只读查看。代码回滚不进行破坏性逆迁移，旧版本不认识新数据时必须阻断写入或使用明确兼容的隔离恢复。

替代方案：每次 tick 发“我检查过了”或恢复备份后自动重放，会造成噪声、泄露和重复收费，故不采用。

## Risks / Trade-offs

- [基础实现存在但生产组合未合格] → capability fail-closed；使用相同候选版本的端到端证据，不勾选其他 change 的任务。
- [现有 JSON/Redis 调度入口不满足本机事务要求] → 先审计 port、持久化与队列合同；复用中立逻辑并增加受测 local adapter，不堆第二套业务 runtime。
- [来源覆盖有限导致漏论文] → 保留按源成功水位和有限 lookback，显示截断/失败/覆盖缺口，不保证未检查区间无论文。
- [持续授权变成无限费用或跨 owner 权限] → 有效期、显式 scope、共享额度预留、逐次权限/fence 校验与撤销竞态测试。
- [取消不等于撤回远程请求] → 区分请求取消、确认终止和 indeterminate；保留费用/receipt 对账，不虚报完成。
- [一台电脑无法全天在线] → 如实显示 runtime 生命周期和下次执行，有限补跑，不隐式引入云服务。
- [保守重确认降低便利性] → 首期优先可审计安全；仅名称等非执行展示字段免重新授权。

## Migration Plan

1. 评审本提案及前置能力矩阵；冻结首期只有 `paper_watch`、本机存活触发、应用内收件箱的边界。
2. 在 default-off 下新增带版本的 owner-scoped 数据与 application ports，关联现有 workspace/conversation/run；旧数据不自动创建 active Routine。
3. 完成本机事务持久化、outbox/inbox、queue、scheduler 与 Harness 接线；跑故障注入、跨 owner、预算和撤销测试。
4. 完成真实论文闭环、记忆边界和工作台，基于 fixture 验证业务规则，再用用户批准的 provider/预算做受控 live 验收。
5. 验证重新启动、时钟/DST、停机、旧备份、功能关闭、取消不确定性与只读 replay；记录输入版本、能力和产物证据。
6. 运行适配范围检查、`python -m scripts.dev compile`、`python -m scripts.dev test`、必需的 `python -m scripts.dev smoke` 与 `openspec validate research-assistant-routines --strict`，通过后逐个显式启用。
7. 回退时关闭功能、围栏化未完成任务并保留审计；禁止自动重放或删除用户资料来获得“干净启动”。

## Open Questions

- 实施前需核定现有本机数据存储、queue 和 Harness invocation port 是否能提供所需事务、幂等查询与 fencing；不能仅因有同名模块就假定满足。
- 首次 live 验收使用哪个已允许的论文数据源、模型以及具体费用预算？未得到用户授权前仅运行离线/fixture 验证，不发起收费调用。
- 产品默认的授权有效期、有限 catchup/lookback、每日论文数和费用限额需在实施验收中固定并可见确认；这些值可配置，但有限、可验证与不得静默扩张的合同不变。
