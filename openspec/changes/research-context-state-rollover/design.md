## Context

本设计承接 [proposal.md](proposal.md) 和 [prd.md](prd.md)。文档中的新增类型、事件与策略名是本变更的目标契约，不表示当前已有实现。首期生产接入为单论文 Paper RAG 多轮补证，现有 `Research Graph + Graph Harness` 的外层路由、质量 gate 和发布边界保持有效。

当前源码依据（2026-09-10 只读核对）：

| 当前实现 | 已确认职责 | 本变更接入方式 |
| --- | --- | --- |
| `framework/harness/rag/session.py::RAGSessionState` | 管理接受、拒绝、冲突证据及预算；controller 包含补证流程 | 提供研究状态投影与逐轮持久化边界，避免复制全部 controller 状态 |
| `backend/research/domain/evidence.py` | `ResearchClaim`、`ResearchEvidenceItem`、`ResearchEvidencePack` 包含论断、引用、来源链和缺失信息 | 在现有业务模型之上增加版本、适用条件、核验范围和依赖关系 |
| `framework/harness/context/assembler.py`、`runtime.py` | 组装、预算检查、受控 compaction、快照和 dispatch 准入 | 从研究状态生成 context groups，继续使用已有验证与物理请求准入 |
| `infrastructure/research/context_runtime.py` | 将 Research materializer 和 compaction runtime 注入 assembler | 装配新策略所需端口，不把领域规则放入 infrastructure |
| `framework/harness/control_plane/graph_state.py` | Graph 节点、等待、预算、事件序号和状态校验和 | 保持控制状态唯一所有者，不塞入论文业务字段 |
| `framework/harness/control_plane/graph_checkpoint.py` | 基于已读 canonical history 的纯 reducer / checkpoint 验证 | 复用恢复协议，业务投影通过已接受记录重建 |
| `infrastructure/storage/postgres/conversation.py::compact_messages` | 把活动消息替换成摘要标记和最近消息 | 启用路径必须先独立归档，不能把活动消息列表当完整原始历史 |

现有 `harness-context-compaction-verification` 任务清单已完成，但这一点不是本变更新能力的运行验证。`model-aware-llm-context-preflight`、`durable-event-runtime` 仍有未完成工作，接入时核对实际所需端口能力，不以整张清单完成度代替 readiness。

## Goals / Non-Goals

**Goals:**

- 让下一轮模型使用明确、有效、带适用范围的研究状态；降低重复输入原文和重复语义核验的成本。
- 对已结构化的条件建立补丁与最终 provider 输入两个保全检查点。
- 对首次抽取遗漏提供按需回读、关键结论核验和诚实的未决输出。
- 完成状态、历史、窗口切换、预算及恢复的真实纵向接入和可复现评估。

**Non-Goals:**

- 不承诺发现所有语义遗漏，也不把“论文声称”升级为“独立复现成立”。
- 不禁止摘要、不要求每个窗口重验全文、不为每条新句子自动创建独立 verifier 调用。
- 不新增全局工作流引擎、向量数据库或独立记忆平台；不让普通 run 修改长期记忆或 active skill。
- 不在本期迁移所有 AgentRunner 会话、并行研究与 Reader Repair；不补造已经丢失的旧原始消息。

## Decisions

### D1. 以既有业务实体为基础，区分五类数据

| 数据 | 内容与所有者 | 权威关系 |
| --- | --- | --- |
| 研究工作状态 | Research 定义目标、论断、条件、证据、缺口、冲突和核验状态 | 来自已接受事件及其不可变 artifact 的业务投影 |
| Harness 控制状态 | Harness 管理阶段、预算、权限、attempt、等待和外部副作用 | 既有 canonical Graph history |
| 模型上下文视图 | Harness 组装本轮任务相关的状态和证据 | 可重建投影，不能反向充当事实源 |
| 历史与交接笔记 | 历史为已准入的原始输入、结果和接受/拒绝记录；笔记从状态生成 | 历史是溯源依据；笔记是可替换的工作视图 |
| 跨任务记忆 | 经独立准入的知识或策略 | 沿用现有 memory authority，与换窗口解耦 |

“原始”指进入系统并通过既有隐私/权限策略的材料，不要求保存凭据、敏感原始秘密或模型隐藏思维链。正文、表格、表注、模型候选、工具参数和工具结果按允许的保留策略存储，并保留证据意义所需的结构。

选择由事件接受记录建立权威，再生成研究投影，避免另建可被模型直接更新的“大状态 JSON”。`RAGSessionState` 仍服务通用检索控制；论文领域模型和语义检查实现放在 `backend/research`，通过端口接入通用 Harness。

### D2. 工作记录的核验身份包含适用范围

新增的版本化工作记录应至少表达：

- `claim_id`、`claim_revision`、命题内容及其含义身份；`source_snapshot_ref`、source revision/checksum。
- 指标、比较基线、实验设置、数据集、已知限定条件及 `scope_status`；条件未知与条件为空必须有不同含义。
- `evidence_refs`、必要原文片段、`dependency_refs`、已确认的适用范围及 `check_record_refs`。
- `assessment`：`pending`、`supported_within_scope`、`insufficient`、`disputed`、`contradicted`、`needs_recheck` 或 `superseded`。

这些是目标契约字段，可按既有模型组织；不为每篇论文生成新 schema。`supported_within_scope` 仅指按当前政策完成了特定范围的证据核对，不宣称绝对真实。

复用键绑定 actor/run scope、命题及 revision、适用范围、依赖版本、证据 checksum、核验政策与 verifier/prompt revision；缓存检索后仍检查当前权限与保留状态。纯 UI 排版变化可以保留同一命题身份；任意自然语言改写不能仅由字符串 hash 或模型自称同义来确定性判为无语义变化。

未记录全部依赖仍可能造成漏检，因此“缓存命中”仅意味着符合复用规则，不是新的正确性证明。

### D3. 补丁是候选，提交遵循先接受记录后投影

Research candidate patch 包含 `operation_id`、`base_state_version`、run/session identity、producer task/attempt identity、操作列表和证据 manifest。操作限定为业务白名单，例如增加证据关联、提出论断修订、记录范围核验候选和缺口候选；禁止任意 JSON path 写入控制面字段。

提交顺序：

1. 已授权工具返回或模型输出先存入不可变 artifact / transcript，获取真实可验证的引用。
2. Research 确定性规则检查结构、已知条件删改、来源范围、引用完整性；语义判断由 worker 提出候选。
3. Harness 校验当前状态、attempt、授权、依赖版本和预算；计算并持久化拟接受的 state artifact，在 VERIFY 后通过 canonical owner 以 expected sequence / CAS 提交接受或拒绝事件。未接受 artifact 不能成为活动状态。
4. 接受事件绑定候选、前后 state version、policy、依赖及 checksum；纯 reducer 生成下一版投影。提交失败时不推进状态，已写但未接受的 artifact 可回收。
5. 进程在事件提交后、投影保存前崩溃时，从接受事件重建；重复 `operation_id` 返回已记录结果，不重复应用。不同内容复用同一操作标识必须拒绝。

检索数量、工具 receipt、计费数据等确定性事实由程序更新。模型不能决定是否进入正式状态、清零预算、改变 Graph 路由、授权工具或发布报告。

### D4. 两类条件遗漏使用不同检查

**已记录条件在补丁或投影中被删掉：** 比较现有语义记录及其 condition association。结论继续使用时，删除、放宽或解除条件关联触发 `scope_change_requires_recheck`；不得把未知条件解释成无限制。若新证据确实支持扩大范围，按新的命题/范围身份重新准入。

**条件首次没有被记录：** 差异比较不能发现未知遗漏。对当前需要使用、范围未核验、产生新推断或影响最终回答的关键结论，按引用读取表格、表头、表注和相关实验说明，执行针对性范围核验。worker 可能漏检；无法确认则保留 `insufficient` 或 `pending`，不宣传“确定性语义完整”。

首次阅读时尽量在同一次 LLM 调用内完成抽取与范围核对，共享已经在上下文中的证据。高影响或含糊结果可触发额外核验；并不为每条结论默认追加一次调用。

### D5. 回查以用途和变化触发，按证据组处理

确定性策略接收事实变化、调用用途和 worker 提出的疑点，产生有原因码的决策。允许的触发包括证据/依赖变化、已知条件删改、范围扩大或新推断、引用无法解析、冲突、首次范围未核验且当前要使用，以及最终核心论断覆盖不足。

读取从已有缓存或精确引用开始；不足时扩到表注和实验设置，再扩到相关章节；不能默认取回全文。已经载入本轮的同版本片段直接复用。核验可按相同来源、实验和适用范围批量执行，每条 claim 单独获得结果，batch 有 token/item 上限。

最终 gate 对关键结论作覆盖检查；存在可复用核验记录则复用，否则只核对缺少覆盖的部分。关键性至少由输出角色及业务规则覆盖主要结论、数值比较、排名、因果和泛化主张，不能完全由生成者自行声明“低影响”来跳过核验。可配置、带预算的抽查用于发现一致性错误；抽查不是完备保证。

同一 `source snapshot + claim/scope + dependency set + trigger` 已完成且没有新证据时，禁止在每个窗口重复发起相同回查。worker 只提出检索建议，Harness 根据授权和预算决定是否执行。

### D6. 冲突保存事实范围，修订沿依赖传播

- 不同指标、数据集或实验条件下的结论分开保留，不能仅比较数值就覆盖。
- 不同论文版本保持独立快照。run 固定来源版本；更换版本必须记录显式 scope revision 并失效受影响记录。抓取时间较新不能代替版本优先规则。
- 同版本同范围的真实矛盾保存双方证据并标记 `disputed`；重复转述按来源血缘去重，不能用多数摘要“投票”。
- 未找到支持只证明已检查范围内证据不足，不自动证明反命题。
- 修订事实使依赖结论进入 `needs_recheck`；禁止依赖其成立的确定性输出，但允许独立任务继续，必要时在答案中披露争议。

冲突无法在预算内解决时，记录未决状态、已查范围、原因码和恢复条件；无新材料或用户范围变化不自动重复补证。

### D7. 窗口切换属于调用上下文重建

```mermaid
flowchart LR
    A[已提交研究状态] --> B[按当前任务选择结论与证据]
    B --> C[结论和条件成组装配]
    C --> D[物理请求预算与保全检查]
    D --> E[记录并激活上下文快照]
    E --> F[Worker 候选或工具建议]
    F --> G[Harness 校验和持久化接受]
    G --> A
    B --> H[按需读取外部历史]
    H --> C
```

`ContextProjectionManifest`（拟定契约）记录 state/event version、selected claim revisions、条件关联、evidence span checksums、缺口、policy/model/tokenizer revision、预算快照和最终 prepared request fingerprint。预算使用实际 provider 请求计数，并包括 system/tool schemas、证据、图像预算和输出预留。

语义单元由“命题 + 已知条件 + 核验范围 + 不确定性”组成；模型需要做新的证据判断时，载入必要原文片段，裸引用不能被宣称为模型已读证据。保护检查必须覆盖物理 materialization 后的最终请求及其 manifest；不能只检查数据库中字段存在。

超预算按策略移除不相关或可恢复旧工具结果，缩小本轮问题范围，按需读取/分步处理，必要时使用已有已验证摘要机制。禁止保留裸结论而裁掉其已知条件；保护内容仍超限时受控拒绝 provider dispatch。

只在当前步骤的工具结果、状态更新和必要 transcript 都已持久化且无待完成工具事务时切换。未决外部副作用不能因切换而被当成未执行；沿用 reconciliation，无法确认时保持等待/停止。`run_id`、conversation identity、权限、预算、retry/replan counter 与操作幂等身份均连续。窗口编号只是视图标识，不创建新业务 run。

### D8. 原始历史与活动窗口独立保留

启用路径在裁剪前提交 archive manifest，覆盖被移出窗口的已准入消息、工具输入/结果、来源快照与必要多模态资产。记录顺序、身份、checksum、引用和缺口；缓存文件或临时路径不满足 durable archive。文档页中的表格图片必须能恢复所需视觉证据，不能只保存不可定位的 OCR 摘要。

只追加历史不意味着永久保存：沿用 actor/tenant isolation、隐私、权限和 retention。数据依法或按保留策略删除后写明 tombstone / unavailable，相关核验记录失效；不能继续声称证据可恢复。

活动消息压缩与 archive 对象可以独立存在，但旧压缩记录缺失原文时仅作未验证历史使用。新策略不得在 archive 失败后静默调用破坏性旧裁剪路径。

### D9. 三层缓存与成本上限

1. 原文/工具结果缓存减少 I/O 和外部请求，但再次输入模型仍计入 token。
2. 结构化抽取与范围核验结果缓存减少重复 LLM 工作；按 D2 身份与有效性复用。
3. provider prompt caching 降低重复前缀的处理费用，命中不减少上下文占用，也不证明语义正确。

每次新 run 的 policy 固定 `max_recheck_calls`、`max_recheck_input_tokens`、`max_recheck_output_tokens`、`max_source_reads`、`max_source_read_bytes`、`max_recheck_attempts_per_issue`、`max_batch_items`、`max_batch_input_tokens` 和可选 `max_verification_cost`，全部非无限；默认关闭周期性全量复核，不强制独立 per-claim verifier。

核验/摘要的消耗是总 Harness 预算的子账本，不能额外创建一份可绕过总限额的预算。调用前预留输入与最大输出预算，调用后按实际 usage 结算；缺失 usage 或超时保持未决 reservation，重启和换窗口不释放它。费用无法可靠计算时报告 unknown，并按 token/call 上限执行，不填零或伪造节省。已配置货币上限而又无法保守估价时，拒绝相关调用，不能用 unknown 绕过货币准入。

批处理、抽查和最终核验都计入同一预算。预算不足时优先关键论断，明确未核验项，收窄输出；核心任务无法满足时给出类型化未完成结果。不得预算耗尽后自动通过 gate，也不得无限回读。

### D10. 验证分为确定性不变量与真实模型质量/成本

确定性 replay 固定记录的候选和工具结果，在各安全点强制 rollover，比较最终接受状态、条件关联、证据身份和预算；不要求随机模型逐字重现。不变记录跨窗口复用时，额外语义核验调用数应为 0；同源批量核验不应重复读取同一可复用片段。

真实模型评估使用固定来源快照、相同模型与工具设置、同一任务集，对比当前已验证 compaction、单纯旧工具结果移出窗口策略（实验基线）与本方案；不得改生产 gate 为测试省 token。记录所有失败/重试、状态抽取、摘要、核验及子任务费用，不能只统计主 Agent。

分开报告：总输入/输出 token、cached/uncached tokens、费用与延迟、重复读取率、核验复用率、限定条件遗漏率、无依据泛化率、引用可解析率、答案完成率和可用性。语义指标按有原文标签的案例评估，标注范围、样本量和误差；结构测试通过不是语义正确率。成本降低必须在质量合格且回答范围可比的完成任务上统计，不能通过提前终止或大量拒答制造节省。

默认上线门槛：确定性不变量全部通过；标注关键范围用例无新增条件遗漏/无依据泛化；真实任务集质量与完成率不低于冻结基线；配对完成任务总 token 与费用分别报告且至少一项降低、另一项不得恶化。达不到门槛时保持 shadow/disabled；此门槛是验收目标，不是当前结果，也不引用外部实验百分比作为项目承诺。

## Risks / Trade-offs

- 首次抽取漏条件 → 按用途触发范围核验、保留 unknown，核心结论覆盖检查与有限抽查；仍有残余语义风险。
- 不完整依赖使缓存错误复用 → 显式来源与范围绑定、保守失效、矛盾触发扩大检查，不把 cache hit 等同真值。
- 频繁切换破坏 provider 前缀缓存 → 以总 token、实际费用和延迟共同评估策略，避免固定每 N 轮强切。
- 原文归档和索引增加存储/I/O → 按 checksum 去重、沿用保留策略；不以删除唯一证据副本换取空间。
- 核验模型与生成模型重复同一偏差 → 针对性原文核对、确定性数值/单位校验、未决出口，不能靠多数模型一致证明正确。
- 多层已有状态重复维护 → 控制状态由 Harness 拥有，Research 仅维护业务投影与引用；notes 单向生成。

## Migration Plan

1. 冻结 V1 caller inventory、source/artifact/durable store 能力与基线；未满足持久化和身份要求的部署拒绝启用。
2. 增加工作记录、补丁与投影契约，先在离线 replay 中验证；旧数据默认 `pending` / unverified，不自动补造缺失字段。
3. 接入 Paper RAG 的真实读取、补证、回答与恢复路径，shadow 生成状态/投影并计量全部额外成本；shadow 结果不授权业务输出。
4. 在候选集合上运行强制切换、提交崩溃、失效传播、预算和源材料删除测试；补充真实模型成本/质量评估。
5. 对新 run 显式启用版本化 policy 并小范围灰度。run 开始后固定策略，不能静默混用新旧投影或恢复格式。
6. 回滚只改变新 run 的策略选择。旧 run 继续按其固定策略受控完成或停在可恢复 checkpoint；保留历史与 artifacts，不借回滚重置预算、清理未决写入或迁回已丢失历史的摘要路径。

## Open Questions

- 模型部署对应的 soft rollover 阈值、每次批量大小和核验预算，由冻结基线及任务分布校准。实施任务须在灰度前登记有限配置及其 policy revision；不能把“待校准”解释成无上限运行。
- 真实调用路径是否都提供 source snapshot、工具 receipt 和完整 usage，需由 caller inventory 确认；缺失能力是启用阻断条件。
- 后续并行分析、Reader Repair 和多论文比较接入时分别新增业务范围与验收，不自动从本期通过推导其已交付。
