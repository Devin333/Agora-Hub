# 契约收敛实施与验收方案

日期：2026-09-23。对应任务：1.2、1.3。规划基线：`01b10aa0`。

本文件是依据现有规格形成的实施方案，不是代码审计报告、已实现契约注册表或发布证据。当前未取得 explorer 的实现定位结果，因此下列实现目录是待核实的职责边界，不能据此宣称现有代码存在或缺少某字段。任务 1.2、1.3 保持未完成。

## 范围与交付物

统一 execution profile、child handle、group/wave/task/attempt、receipt、budget、event、artifact、continuation 之间的精确版本、身份绑定及读写策略；在实际 admission、result acceptance 和历史读取边界调用确定性校验。

交付物包括：现有类型与调用方清单、由现有定义派生的版本契约清单、必要的类型与调用方修改、边界校验和针对性回归证据。若契约一致性必须依赖后续 coordinator 或 continuation 行为，应记录依赖并保持相应任务未完成。

本批不承担 provider 部署资格、多池调度、完整 parent continuation 或 Research 并行发布；这些分别由任务 2.6、4、5、6、7 负责。并行默认关闭。

## 权威归属与约束

“一个版本契约”指一组精确、相容的既有契约及其跨边界不变量，不要求把所有持久化对象改成一个大对象。清单应从现有常量或注册定义生成、验证，避免引入另一套可独立修改的 schema 权威。

| 契约面 | 保留的权威 | 本批需要明确的关联 |
| --- | --- | --- |
| Execution profile 与 provider admission | ExecutionEnvironment 的既有契约及 Harness admission | 精确 profile/policy/capability 身份与本次 Graph、activity、attempt、预算授权的关联 |
| Child handle 与 lifecycle receipt | Harness supervisor 的 canonical transactional lifecycle 边界 | parent/child/run/tenant/task/attempt/operation，以及受保护 admission resource 的 scope、generation 和 lease |
| Group/wave/task/attempt | 已接受 TaskPlan 与 Harness coordinator | plan version/checksum、group membership、wave、task instance、worker binding、join policy、budget envelope |
| Transcript 与 candidate output | SubAgent durable transcript store | 原子 bundle、exact receipt、候选内容 checksum、持久化读回和确定性 gate |
| LLM budget | `framework/governance/budget` 的现有 ledger | run/scope、policy digest、operation/reservation、usage 和 settlement 引用 |
| 其他预算与占用 | 各自原有 owner | TaskPlan capacity、物理 child occupancy、retry、tool/wall-time 和 LLM usage 分别引用，不能互相替代 |
| Runtime event | canonical durable event runtime | envelope 与 data schema、event identity、store sequence、bounded references、redaction |
| Artifact 与 side-effect receipt | canonical artifact 与 side-effect owners | tenant/run/attempt、ref、checksum、bytes/media，以及原有 receipt 和确定性状态 |
| Parent continuation | Harness 的 durable submission 与 gated observation | submission identity、原 parent turn、terminal observation version/checksum 和重复投递身份 |

统一契约不得自行修改预算、释放 occupancy、写入 memory、批准 tool、判定 publication 或创建新的 event/transcript/artifact store。Worker 内容不能授予上述权限。Interface 层继续通过 application service。

## 版本与身份规则

1. explorer 先记录每类对象实际写入、读取和 checksum 计算所使用的精确 schema。采用现有版本策略；有不兼容字段或语义变化时增加版本并明确 reader 策略，不能仅给旧数据改标签。
2. 跨边界关系以受信任的已接纳记录为比较基准。不得仅比较两个都来自 worker 的字段，也不得把不相关的资源 generation、Graph sequence 或 task attempt 当成同一 fence。
3. 每个 serialized object 只承载其职责所需身份；关联通过既有 immutable refs 和 exact checksums 校验。不得把所有父上下文或 sibling transcript 复制进 child 输入。
4. 同一 operation/event/receipt identity 的相同内容遵守对应 owner 的既有幂等语义；冲突内容拒绝且保留原事实。实时幂等重试与历史日志的重复/乱序校验不能混为一谈。
5. 旧数据只能恢复实际已记录的事实。不得补造 transcript receipt、artifact read-back、lease、fence、termination confirmation 或预算授权；缺失时按现有 typed diagnostic 隔离或拒绝执行。
6. 只保留现行规格明确要求的历史 reader；不新建双写、双执行或 moving-version fallback。
7. 同时存在正常 terminal status 与未确认 termination 时，必须保留 `INDETERMINATE` 及未释放占用；控制器退出不代表 provider 已终止。

## 职责与实施顺序

当前会话的 `spawn_agent`、`list_agents`、`send_message` 返回 `unsupported call`。以下是待恢复调度后执行的分工，本文件不宣称已经委派。

1. **explorer：提供当前代码证据。** 搜索 `framework/execution_environment`、`framework/harness`、`framework/governance/budget`、`framework/events`、`framework/tool` 及关联 composition/tests；输出类型与文件行号、版本常量、serializer/reader、checksum projection、owner port、生产调用方、现有验证点。现有规范记录与最新代码应分别列出，确认差异后才形成修改清单。
2. **planner：批准实现边界。** 对上述清单逐项决定“引用现有定义、收敛重复定义、增加必要字段/版本、补充跨对象校验”；列明每个拟修改文件与职责，确定任何后续任务依赖，避免一次变更替换全部存储或状态机。
3. **worker：完成契约与调用方迁移。** 从基础类型和版本策略开始，保持原 owner；随后按依赖顺序更新 admission、transcript/result、projection/continuation 的现有接入点。新增验证函数必须被真实路径调用，不能只供测试使用。共享类型由一个 worker 串行负责。
4. **worker：完成校验与回归。** 覆盖 schema、身份、checksum、tenant/scope、capability/policy/ref 和 transition，并验证失败发生于 dispatch 或 acceptance 之前。已有测试优先复用；新增测试针对行为与权威边界。
5. **explorer 与 planner：验收。** explorer 提供最终 diff 与真实调用链证据；planner 根据下表确认生产路径、失败行为、历史处理与权限边界。worker 修复发现的问题并重跑受影响检查。
6. **主控：整合与提交。** 运行适当检查和强制 smoke，更新证据，仅在完整满足标准后勾选任务并提交。已有未跟踪 `outputs/` 不属于本次变更。

规划与验收使用 planner（gpt-6-astra，ultra），编码和测试实现使用 worker（gpt-5.6-sol，high），只读代码定位使用 explorer（gpt-5.6-luna，medium）；遵守可用并发和嵌套限制。

## 验证矩阵

| 场景 | 必须观察到的行为 |
| --- | --- |
| 正确的 admission -> child -> receipt -> result 关联 | 经现有 owner 校验并完成真实调用路径，身份和 checksum 可追溯 |
| run/tenant/parent/group/wave/plan/task/attempt/binding 任一漂移 | typed rejection；不调用新 worker，不接受结果，不改变原记录 |
| schema 缺失、未知或不兼容；不受允许的字段 | 新请求拒绝，历史按明确 reader 策略隔离，不能自动套用最新版 |
| 内容损坏但 identity 不变；checksum 与 ref 不匹配 | 由实际 owner 读回校验失败；字符串相等不算有效证据 |
| 重复 operation/receipt 的相同内容与冲突内容 | 分别符合 owner 的幂等和冲突语义；不增加调用计数或覆写原结果 |
| 缺失 capability、policy、budget 或 artifact/transcript owner | 在 dispatch/acceptance 前 fail closed；serial 不绕过同一依赖 |
| 非法状态转换、过期 owner、未确认取消 | 受保护写入拒绝；不错误释放 occupancy；不自动重试不确定副作用 |
| 历史记录不足以恢复新字段 | 明确不可恢复诊断；不补造证据，不调用 live worker 修补历史 |
| 离线读取/重建契约事实 | 不调用 live provider/tool/worker/scheduler；已存 schema/checksum 可复核 |
| 合法旧 single-child 调用 | 保持现行规格要求的结果、错误、停止与恢复语义；不为普通非 subagent task 伪造 transcript |

完成代码后，先运行实际变更覆盖的 Harness、SubAgent、TaskPlan、ToolRuntime、budget/event 和 architecture 测试。提交前必须通过 `python -m scripts.dev smoke` 和 `openspec validate harness-runtime-execution-and-parallel-agent-orchestration --strict`；根据集成范围运行 compile/full test。记录命令、退出码、测试结果和所验证 commit/工作区；失败必须根因修复。任务 7.2 的最终全仓与发布前置验收仍保留。

## 完成标准

- 任务 1.2：所有列出的契约面都有经过代码核实的精确版本、writer/reader/owner 和身份映射；真实接入点已采用收敛后的定义；不存在新增重复权威；兼容/拒绝策略有回归证据。
- 任务 1.3：完整验证矩阵在对应真实边界执行并通过，拒绝路径不触发未授权调用、不改写既有事实；有关 architecture 与 smoke 检查通过。
- 本地契约完成不等于 provider 部署、Research parity 或 rollout qualification；相关 2.6、4、5、6、7 任务仍按各自完整标准验收。

## 本次实际检查

- 已读取 apply CLI 返回的 proposal、design、spec 和 tasks；apply 状态为 `ready`，进度 6/39。
- `openspec validate harness-runtime-execution-and-parallel-agent-orchestration --strict`：退出码 0，通过。
- 本轮未修改代码、未实现测试、未取得新的生产调用链证据；没有将任务 1.2 或 1.3 标为完成。

