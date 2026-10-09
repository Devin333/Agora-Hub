# Research 证据可信性与上下文质量基础：实施任务

更新日期：2026-10-10。需求来源为本目录 `prd.md`、`design.md` 和 `specs/`。

## 使用与验收规则

- 本清单把原 27 项任务拆成可单独实现、检查和追踪的工作，不扩大已合并 PRD 的产品范围。
- `[x]` 表示已完成并有证据；`[ ]` 表示尚待验收，可能已有代码。代码存在不等于集成验证通过。
- 原文档迁移和触发核查的完成记录保留；其余条目按实际章节验收结果逐项更新。未通过验证的代码、测试草稿或文档说明不能批量标记完成。
- 顺序：P0 评测合同、输入合同和 Claim 资格 → P1 默认关闭的候选试验 → 集成验证。按大章节推进：先完成第 2 节全部代码，再集中测试、修复和验收；通过后进入第 3 节，后续章节依次执行相同步骤。全量测试安排在第 7 节；既有后续代码保留，待轮到对应章节时验收。
- 第 6 节标记“触发后”的任务属于 P2，只有真实证据满足准入条件才执行；没有触发证据时保持待办，不阻塞 P0/P1 验收。
- 每项验收记录应包含相关代码/测试位置、实际检查结果及必要的失败诊断；生成日志沿用 `.newsroom/`，不建立新的运行库或晋级服务。

## 章节交付边界与验收入口

本清单共 97 项：90 项属于本轮无条件交付，7 项（6.6–6.12）必须等待真实触发。细化的是实现、失败处理和验证工作，不能把原 PRD 中明确暂缓的完整平台重新加入本轮。

| 章节 | 任务数 | 需要交付的具体结果 | 主要验收入口 |
| --- | ---: | --- | --- |
| 1 | 6 | 唯一合并 PRD、替代关系、删除边界及当前 Graph 依赖 | 文档引用、历史迁移和 OpenSpec 严格校验 |
| 2 | 15 | 版本化固定案例、冻结阈值、逐例诊断、成对指标、代码/配置可追溯性及现有 CI 集成 | `rag/evaluation`、固定 CLI/CI、离线复现；证据见 `evidence/evaluation-contract-20261009.md` |
| 3 | 17 | profile 语义、来源/actor 校验、可信资格引用、不可变投影、最终 provider 请求保留及默认关闭 | domain/application、TaskPlan/AgentLoop、真实 provider 组装和架构检查；证据见 `evidence/input-contract-20261009.md` |
| 4 | 18 | 真实表格 V1 Claim、严格单位/条件、四类资格、来源重放、最终报告绑定及精确 Graph/gate 身份 | resolver/service/gate、成功与缺证动态路径、durable transcript/recovery；证据见 `evidence/claim-eligibility-20261009.md` |
| 5 | 14 | 默认关闭的有界候选检索、硬约束与越权拒绝、可执行原始计划回退、血缘及收益结论 | planner/adapter/session/serialization、固定语料成对比较 |
| 6 | 12 | 本轮交付进度观察和触发审计；连续性代码需先满足真实窗口/条件/成本触发 | gate/trace 中的无控制权观察、持久化轨迹核查；证据见 `evidence/context-trigger-audit-20261008.md` |
| 7 | 15 | 端到端、预算与恢复、固定评测、全量与 smoke、默认开关、回滚、证据及提交 | `scripts.dev`、OpenSpec 严格校验、实际运行记录及 Git 提交 |

每章先写完实现和测试代码，再集中运行本章检查、修复根因并验收。第 7 节再运行全量测试与最终提交门禁。某一章的单元测试通过不能代替最终动态路径、全量测试或发布资格；固定指标没有收益也不能自动开启候选试验。
## 1. 文档收敛与遗留清理

- [x] 1.1 在统一 PRD 中列出七份 Research proposal 的替代关系、保留价值和删除边界。
- [x] 1.2 明确 P0/P1/P2 顺序、显式启用范围及不建设平行平台等非目标。
- [x] 1.3 将测试元数据中已废弃 Workflow-era Graph proposal 引用迁移到 canonical `harness-graph` 合同。
- [x] 1.4 删除七份被合并的 Research proposal 活跃目录，不保留兼容占位文档。
- [x] 1.5 删除三份 obsolete Workflow-era change 目录，保留版本控制历史作为审计来源。
- [x] 1.6 将 `durable-event-runtime` 未完成部署资格收缩到当前 Graph 的回滚、重放、安全和投影重建证据，移除 Workflow 兼容前置条件。

## 2. P0：固定评测基线与 CI 合同

- [x] 2.1 清点现有 `backend/research/rag/evaluation`、固定语料、golden set、CLI 和 CI gate，记录复用入口及输出位置。
- [x] 2.2 定义候选检索与输入/Claim 固定案例集的版本和稳定 case ID，明确每例输入、预期结果和失败原因。
- [x] 2.3 冻结约束保持、回退准确率、控制字段拒绝、质量退化和候选收益的阈值及配置版本。
- [x] 2.4 添加输入完整性正例，以及目标/硬约束/来源关联在最终模型请求中丢失或篡改的反例。
- [x] 2.5 添加实体、时间、来源、语言、版本、度量、否定及显式硬约束保持案例。
- [x] 2.6 添加来源 locator、来源修订、证据 checksum 和条件不匹配案例，证明数学自洽不能替代来源核验。
- [x] 2.7 添加 supported、contradicted、insufficient、unavailable 四类 Claim 资格案例及报告投影篡改反例。
- [x] 2.8 添加真实 planner 执行的无效候选、worker 故障、超时及预算耗尽回退案例，核对实际返回计划。
- [x] 2.9 添加 worker 路由、质量、记忆、工具、发布和资格字段越权案例，覆盖嵌套字段。
- [x] 2.10 为每例输出 expected/actual、稳定 reason code、来源定位、资格引用和回退原因；统一诊断字段。
- [x] 2.11 扩展现有报告，记录案例/配置版本、仓库修订、受测核心文件逐文件及聚合 checksum，支持未提交代码的精确追溯。
- [x] 2.12 分别执行固定语料的原始基线与获准候选检索，记录 Hit Rate、evidence/type/locator coverage、citation accuracy 和 MRR 的实际值与差值。
- [x] 2.13 将输入、Claim、约束、引用和回退失败合并到现有 CI gate；缺失指标必须显式失败，不能以默认满分代替测量。
- [x] 2.14 区分质量通过与 `rollout_eligible`：无退化但未达到收益门槛时保持默认关闭，报告原因。
- [x] 2.15 验证固定案例无需外部 source/model 服务即可复现，复用既有 CLI/CI，不增加平行 evaluator、run store 或 promotion service。

## 3. P0：受控输入合同与真实模型边界

- [x] 3.1 定义默认关闭的 `research_input_quality_enabled` 选项，拒绝非布尔值，并限制为已配置动态 runner 的单篇论文路径。
- [x] 3.2 在 Research application service 中接受显式 `ResearchInputProfile`，或从目标、约束和来源修订准备 profile。
- [x] 3.3 验证恰好一个 `CURRENT_TASK`，并核对当前任务与 profile 目标、任务修订的一致性。
- [x] 3.4 保留五类输入的类别、状态、来源、修订、条件和内容引用；拒绝不合法的类别/状态组合。
- [x] 3.5 校验 tenant、user、paper 和来源范围；仅 user 身份的请求沿用现有 canonical tenant 规则。
- [x] 3.6 对来源材料校验来源引用及修订，并在 source 接受边界再次核对实际载入来源，防止错误材料进入 worker。
- [x] 3.7 通过可信 qualification port 核验 `VERIFIED_CONCLUSION` 的资格、来源及精确 gate 版本；缺少验证服务或引用不匹配时拒绝。
- [x] 3.8 保持作者声明、模型候选、工具观察和已核验结论的状态边界，拒绝候选自报 verified 状态。
- [x] 3.9 对 profile 和 prepared payload 建立不可变投影与 checksum，覆盖序列化/反序列化及嵌套数据篡改检查。
- [x] 3.10 让通用 `PromptBuilder` 接收不透明 prepared projection，不解释 Research 类别、来源或资格语义。
- [x] 3.11 将投影沿 TaskPlan → child runner → AgentRunner → AgentLoop 传递，避免把投影当作普通 dependency output。
- [x] 3.12 在首次请求、工具后续请求和重试请求中保留投影；最终逻辑请求校验完整块、目标、硬约束和来源。
- [x] 3.13 将投影接入 Research candidate adapter 与真实 provider 请求组装，防止内层模板或上下文处理丢弃已验证输入。
- [x] 3.14 对错误 profile、未知 gate、投影缺失或歧义 fail closed；证明拒绝发生在相应 source/worker dispatch 之前，且无发布副作用。
- [x] 3.15 在 run metadata 中记录 profile ID/revision/checksum、tenant 和资格 gate 版本，沿用既有 trace/event 所有者。
- [x] 3.16 覆盖默认关闭行为、JSON 转义、非法 profile、来源修订、资格引用和实际动态调用链的单元/集成测试。
- [x] 3.17 增加架构检查，证明框架不依赖 Research，`backend/research` 不依赖 interfaces、infrastructure 或 legacy paper_radar。

## 4. P0：V1 Claim 的来源绑定与发布资格

- [x] 4.1 固定 V1 为 `numeric_comparison_result_v1`，记录适用表格、比较对象、条件、指标和明确不支持的范围。
- [x] 4.2 定义 claim identity/revision、类型化数值比较、source locator、observation、review binding 和 eligibility decision 合同。
- [x] 4.3 从实际表格列头和行数据提取对象、指标、Dataset/Split；对缺列、歧义、跨条件数据拒绝生成合格比较。
- [x] 4.4 严格解析百分比单位和 source scale，覆盖 `%`、percent/fraction、NaN/inf、越界及单位冲突，不依据数值大小猜测缩放。
- [x] 4.5 为比较生成稳定 claim ID、cell locator 和确定性文本，绑定差值、比较关系、单位与适用条件。
- [x] 4.6 在 opt-in evidence builder 中生成真实 table evidence、span refs 和 claim refs；默认路径维持原行为。
- [x] 4.7 在 application service 中仅组合已接受 source/document/evidence pack，核对 paper、actor scope、source hash、revision 和 checksum。
- [x] 4.8 将 parser/worker/人工 review 保持为 candidate observation，由确定性 resolver 解释，不能接受其自报发布资格。
- [x] 4.9 定义四类资格结果的业务含义；仅 supported 可获得资格，其余状态保留缺口或失败原因。
- [x] 4.10 对重复 observation 确定性去重；同 ID 冲突、条件冲突及不一致 observation 必须显式处理。
- [x] 4.11 注册精确 `ResearchClaimEvidenceEligibilityGate@1`，保留 `ClaimEvidenceGate@1` 引用验证依赖。
- [x] 4.12 gate 从 Graph 已接受输出恢复可信 source/document/evidence pack，重放 resolver 并核对 bundle/decision，拒绝伪造来源和过期修订。
- [x] 4.13 构造最终 qualified claim 投影，绑定 claim ID/text/revision、evidence IDs、资格、resolver/gate、locator 和证据修订。
- [x] 4.14 gate 核对实际 `claim_models` 与资格投影一致；正确 bundle 搭配篡改报告文本、重复资格或错误引用时拒绝。
- [x] 4.15 让最终 `ResearchAnalysis.claims` 和实验结论包含同一受控 V1 Claim，其他 worker 内容不自动继承 verified 资格。
- [x] 4.16 为 opt-in 路径建立精确 Graph identity，并在 verify_claims 实际绑定新 gate；缺失 V1 结果必须进入确定性缺证处理。
- [x] 4.17 同步 run spec、context identity、TaskPlan stage、RAG memory admission、结果提交和 recovery 对所选 Graph 的精确引用。
- [x] 4.18 覆盖支持、反驳、不可用、缺证、条件冲突、重复 observation、伪造来源、越权字段和最终报告篡改回归。

## 5. P1：默认关闭的检索候选试验

- [x] 5.1 定义版本化 experiment 配置及严格参数校验；显式非法值不能静默丢弃，配置及嵌套约束在使用期间不可变。默认关闭，仅注入 worker 不应启用试验。
- [x] 5.2 复用现有 `WorkerRAGPlanner` 和 deterministic planner，保留原始 query 及可执行、可序列化的原始计划。
- [x] 5.3 限制候选查询数、实际 RAG 查询预算、worker 调用次数、transport attempts 和 timeout；沿用现有 provider deadline，并核对真实 transport 收到和遵守的预算。
- [x] 5.4 限制 V1 候选为保留原查询的受控扩展，仅允许配置的 expansion terms 或受控 evidence types。
- [x] 5.5 核对实体、时间、来源、语言、版本、度量、否定及额外显式约束，拒绝遗漏、替换和否定反转。
- [x] 5.6 覆盖 Unicode/CJK 和精确词边界，拒绝 Atlas/AtlasX、v1/v10、95/195 及允许词拼接成新词的误接纳。
- [x] 5.7 限制候选 operation 和 corpus 范围，拒绝借候选提议额外 source reading、memory recall 或控制操作。
- [x] 5.8 递归拒绝 routing、quality、memory、tool、publication 和 qualification 权限字段，包括嵌套 metadata、对象形式候选及模型适配器顶层输出；不能先丢弃越权字段再视为有效。
- [x] 5.9 对缺失/非法/超量候选、worker 故障、真实 provider 超时、预算耗尽、验证失败及验证器异常返回原始确定性计划；原始计划可执行时不因候选越界额外终止。
- [x] 5.10 为接纳和拒绝记录稳定 reason、fallback reason、原始 query、candidate ID、实际配置与版本，避免泄漏 provider 原始异常。
- [x] 5.11 将 lineage/config metadata 贯穿 session policy、retrieval request、query planner 和序列化 `RetrievalPlan`。
- [x] 5.12 覆盖默认关闭零 worker 调用、显式启用、参数限制、错误回退、序列化和执行原始计划的回归测试。
- [x] 5.13 使用第 2 节固定语料进行成对比较，记录无退化和收益结果，不以候选准入成功替代检索质量。
- [x] 5.14 按冻结阈值记录有限启用或保持关闭的结论；扩大查询/来源范围前必须提交对应评测证据，不能自动开启。

## 6. P2 触发式上下文连续性与进度观测

- [x] 6.1 定义稳定进度原因码和有限数值指标，包括 `insufficient_verified_progress`；分类状态与数值指标分开，关联当前 run/paper/claim（存在时）与证据上下文，覆盖不可变、序列化和 checksum。
- [x] 6.2 将观察记录放入既有 gate/trace/event 输出，验证其可诊断但不能独立路由、发布、自动停机或写入记忆。
- [x] 6.3 定义窗口丢失、条件漂移或重复读取成本的触发证据要求，禁止在没有真实问题证据时启动连续性实现。
- [x] 6.4 清点本地持久化轨迹，记录样本范围、校验身份、发现和局限；已形成 `evidence/context-trigger-audit-20261008.md`。
- [x] 6.5 根据当前核查记录“证据不足，暂不启动”的决定，保留后续触发待办，不以模拟生产轨迹代替真实触发。
- [ ] 6.6 【触发后】记录问题 run/source/revision、真实窗口前后证据、损失或重复读取成本及相对基线，审查是否满足准入条件。
- [ ] 6.7 【触发后】为单 run 定义已接受证据引用、适用条件和来源修订的工作投影，指定既有 context/transcript/artifact 所有者。
- [ ] 6.8 【触发后】在真实模型窗口边界执行受控重组，记录窗口序号、投影版本与校验身份。
- [ ] 6.9 【触发后】处理来源修订或条件变更，拒绝旧资格静默继承，必要时通过现有 deterministic gate 重新验证。
- [ ] 6.10 【触发后】接入既有 transcript/event/artifact 进行回放和恢复，不新增 archive、CAS、claim-state 或全局缓存。
- [ ] 6.11 【触发后】增加显式默认关闭配置，限定单 run 使用和回退行为，禁止跨 run 长期记忆扩展。
- [ ] 6.12 【触发后】覆盖真实窗口重组、条件保留、来源修订、重放、默认关闭和失败回退测试，并与触发前基线比较。

2026-10-08 核查的 119 份本地持久化记录未提供合格触发事件。截至本次清单细化，6.6–6.12 保持条件待办；该结论不等于证明生产环境不存在上下文问题。

## 7. 集成验证、发布控制与交付

- [x] 7.1 编译本次涉及的生产模块，修复语法、导入和合同接口问题。
- [x] 7.2 运行输入、Claim、retrieval、AgentLoop、TaskPlan 和架构定向测试，记录实际结果并修复失败根因。
- [x] 7.3 验证动态成功路径从 source collection 到 artifacts/storage，核对最终报告 Claim 与 gate qualification 一致。
- [x] 7.4 验证无 V1/缺证路径实际进入新 gate，执行有界失败/replan/halt 且不发布；前置 child runtime 失败不能充当 gate 拒绝证据。
- [x] 7.5 排查集成中 TaskPlan 子任务组失败的真实 receipt/transcript 原因，修复顺序或并发相关缺陷后复验成功和缺证路径。
- [x] 7.6 验证 `PLAN -> EXECUTE -> VERIFY`、max_replans/max_turns/retry budget，证明 worker 与进度观察均无控制权。
- [x] 7.7 核对新增 phase transition 的完整性、持久化和 recovery/replay；精确 Graph/gate 身份不匹配时拒绝恢复。
- [x] 7.8 通过既有 `python -m scripts.dev test-rag-eval-gate` 跑固定基线，记录案例版本、代码 checksum、逐例结果及成对指标。
- [x] 7.9 完成本轮代码后运行 `python -m scripts.dev test` 全量测试；如失败，修复根因并复验，不跳过或削弱断言。
- [x] 7.10 运行提交前必需的 `python -m scripts.dev smoke`，覆盖 Harness/Research/API/service、架构和 source validation。
- [x] 7.11 运行 `openspec validate research-evidence-context-quality-foundation --strict` 和 diff/文档引用检查。
- [x] 7.12 记录 feature flag 默认状态、部署范围、精确 Graph/gate、V1 表格限制、基线版本和阈值。
- [x] 7.13 记录失败回退、关闭新请求开关、候选恢复原始计划和代码回滚步骤；保留在途 run 身份及审计证据。
- [x] 7.14 形成验证证据，区分测试通过、部署资格和候选收益；记录 P2 未触发及未实现项，避免声称全部完成。
- [x] 7.15 按实际结果更新本清单及 PRD 状态，检查新增文档是否受 ignore 影响，提交本次范围内代码与证据。

## 合并前 PRD 的需求去向

下表用于核查需求覆盖；一份旧 PRD 的多个任务可能共享本清单中的同一实现，不按旧任务数量机械累加。

| 旧 PRD | 本清单中的保留价值 | 暂缓或舍弃的范围 |
| --- | --- | --- |
| `research-agent-input-contract` | 3.1–3.17：输入语义、可信资格、受保护投影、最终模型边界和架构检查 | 全 provider、全部历史 RAG/OCR 和非 Research 路径迁移 |
| `research-evidence-review-resolution` | 4.1–4.18：V1 类型化 Claim、来源绑定、四类资格、确定性 gate、报告与恢复；相关固定反例见第 2 节 | 完整争议工单、独立盲审队列、规则发布系统及复杂复核界面 |
| `research-semantic-verification-reading-latency` | 2.6–2.7、4.1–4.18：从真实来源定位和条件核验开始，证明数值自洽不等于证据成立 | 将 V1 局部能力扩展成全量语义验证或全论文 reader 重建 |
| `add-research-evaluation-harness` | 2.1–2.15、7.8：版本化离线案例、可比指标、失败诊断及既有 CI 集成 | 平行 evaluator、run store、全局 dataset/promotion 平台 |
| `paper-rag-hybrid-query-rewrite-v1` | 5.1–5.14：默认关闭、受限扩展、硬约束校验、血缘、原始计划回退和成对收益门槛 | 默认启用的通用查询改写、澄清状态机、跨任务缓存和持久候选日志服务 |
| `research-progress-governance` | 6.1–6.2：稳定原因码、指标和既有 trace/event 中的诊断 | 由进度指标自动决定停机、路由、发布或记忆写入；强制治理须另立变更 |
| `research-context-state-rollover` | 6.3–6.5：真实触发核查；6.6–6.12：触发后限定单 run、真实窗口的续接 | 无证据提前启用；平行 archive/CAS/claim-state、跨 run 长期记忆及全局缓存 |
## 原清单映射与当前验证说明

| 原任务 | 细化后范围 |
| --- | --- |
| 1.1–1.4 | 1.1–1.6 |
| 2.1–2.4 | 2.1–2.15 |
| 3.1–3.4 | 3.1–3.17 |
| 4.1–4.4 | 4.1–4.18 |
| 5.1–5.4 | 5.1–5.14 |
| 6.1 | 6.1–6.2 |
| 6.2 | 6.3–6.5 |
| 6.3–6.4 | 条件任务 6.6–6.12 |
| 7.1–7.3 | 7.1–7.15 |

按章节验收：第 2 节已完成，111 项定向及受影响合同回归通过，54 个固定案例通过，两次离线 CI 报告 SHA-256 一致；证据见 `evidence/evaluation-contract-20261009.md`。候选 Hit Rate 增量为 0，未达到 0.01 的启用门槛，保持默认关闭。第 3 节已验收：动态 16 项通过，基础检查首轮 481 通过、3 失败，根因修复后受影响 135 项全部通过；证据见 `evidence/input-contract-20261009.md`。第 4 节已验收：268 项基础回归通过（2 项既有 Windows symlink 权限跳过），动态套件修复后 34 项通过，受影响成功及缺证路径最终 2 项通过。缺证路径实际进入新 gate、耗尽允许的一次 replan 后持久 HALT 并禁止发布；证据见 `evidence/claim-eligibility-20261009.md`。第 5 节已验收：首轮 1,215 项通过、3 项失败，根因修复后受影响复验 218 项全部通过；54 个固定案例再次通过，候选收益仍未达门槛并保持关闭。证据见 `evidence/candidate-retrieval-20261009.md`。第 6 节进度观察已验收：144 项定向及 2 项动态路径通过；触发审计已完成，6.6–6.12 保留条件待办。证据见 `evidence/progress-observation-20261010.md`。第 7 节最终检查已通过：首轮全量 8,949 通过、2 失败；修复 Windows 锁初始化和 planning admission 时间戳的并发根因后，受影响 121（2 项既有跳过）与 60 项复验通过，最终从头全量 8,966 通过、115 既有跳过、24 deselected、0 失败。提交前 smoke 4,188 项通过，离线 AgentLoop 与 source validation 通过；compile、固定 54 案例和 OpenSpec 严格校验通过。最终源码报告追踪 58 个核心文件。详细证据见 `evidence/final-verification-20261010.md` 和 `evidence/verification-results-20261010.json`；代码、测试和证据作为本轮提交交付。当前共 90 项验收，6.6–6.12 共 7 项等待真实触发，未声称 97 项全部实现。
