## Context

本变更针对 Research PDF parsing，不重建整个 RAG 或研究报告流程。产品需求与历史数据在 [PRD](F:/github/Agora-Hub/openspec/changes/research-pdf-parsing-quality-optimization/prd.md)，变更范围在 [proposal](F:/github/Agora-Hub/openspec/changes/research-pdf-parsing-quality-optimization/proposal.md)。设计日期为 2026-10-21；以下组件和文件名中未存在的部分均是拟议实现，不是当前能力声明。

### 当前实现证据

| 现有位置 | 已观察行为 | 设计影响 |
| --- | --- | --- |
| `F:/github/Agora-Hub/backend/research/document/marker_pdf_parser.py` | `_flatten_blocks` 展开父子 block；`_components_from_blocks` 分别积累 sections/equations/tables；section flush 使用同一个 current_page 作为起止页 | 保留树节点身份/所有权，显式表示混合 block 顺序和真实跨页覆盖，先测试重复输出/错页而非臆测根因 |
| 同文件 `_block_text`、`_block_equation_text`、`_TextHTMLParser` | text、inline equation 和 display equation 经 HTML-to-text/空白规范化进入不同实体 | 公式分隔、上下文和 raw 内容需按类型保留；解析器不能将任意含尖括号 LaTeX 当 HTML 清洗 |
| 同文件 `_TableHTMLParser`、`_matrix_to_rows` | 当前 cell 文本投影不保留 span；矩阵会过滤空 cell；首行转为 columns，重复列名改名 | cell-grid 是规范结构；dict rows 仅是派生视图，不能替代原始位置/表头 |
| `F:/github/Agora-Hub/backend/research/domain/document.py` | Research entities 和 source lineage 已存在；无显式跨类型 block 顺序主干 | 扩展 domain，而不是建立 benchmark-only 文档模型 |
| `F:/github/Agora-Hub/backend/research/document/chunker.py` | 正文、公式、图和表分别构造 chunks，已有公式引用、表格 parent/row-group 和 locator 逻辑 | 复用现有 chunks 类型和引用，新增 fidelity mapping；不重写 chunker 或检索策略 |
| `F:/github/Agora-Hub/backend/research/document/cascade_parser.py` | 已有 `DocumentQualityProbe`、parser attempts、PyMuPDF admission 和 cascade | 扩展同一 deterministic probe/report，路由仍由现有控制者执行 |
| `F:/benchmarks/AgoraHub-OmniDocBench/scripts/run_project_parser.py` | raster page → batch PDF；注入直接 subprocess runner；评分导出重新读取 raw JSON；存在 page clamp | 该执行身份是历史诊断，不是 production Harness run；正式 adapter 不复制此旁路 |

读到的当前工作树包含并行进行的改动，以上不是对历史 benchmark 所用源码 commit 的追溯证明。M0 必须分别保存历史证据和新的 source/config 指纹，不能把当前 HEAD 自动填进旧结果的 provenance。

## Goals / Non-Goals

**Goals:**

- 对公式、表格和阅读顺序建立可追踪、可复测的生产文档保真合同。
- 先测量 backend → domain → actual chunk 的信息损失，再修复被证实的转换问题，并只在必要时调用重解析能力。
- 保持当前 source → evidence → analysis → report → quality gate → storage 路径、既有执行预算/receipt、后端选择和应用层边界。
- 用正确分母、失败账本、冻结基线/阈值和独立文档集证明质量收益；无有效证据不允许新 profile 晋级。

**Non-Goals:**

- 不训练或默认更换 OCR/LLM，不重引 MinerU，不改变 LaTeX-first source selection，不在 domain 中依赖 interfaces/infrastructure。
- 不把 benchmark gold、人工修正答案、split label 或目标分数送进生产 parser/recovery；不创建 agent 来做排序、HTML grid 转换或验收决定。
- 不新建 store、通用 benchmark 系统或第二个 RAG evaluator；不全面迁移 HTML/LaTeX parser 到新 PDF 结构。
- 此次只写方案，不改业务实现、配置、测试数据和既有评测 artifacts。

## Decisions

### D1. 先建立两个评分轨，再检查真实下游消费

| track | 内容来源 | 可回答的问题 |
| --- | --- | --- |
| `backend_raw` | 已保存的、具备版本身份的原始 backend artifact；导出器只做格式序列化 | backend 识别能力及 evaluator/export 问题 |
| `production_document` | 实际应用 parser 输出并持久化的 ResearchDocument 及其规范 artifact | 产品真正保留了什么，normalization 是否丢失 |
| `downstream_projection` | 同一个 document snapshot 经真实 chunker 生成的 chunks / parent payload / locator mapping | 内容是否在切块或存储边界丢失；是项目自定义完整性检查，不冒称官方 OmniDocBench 指标 |

B0 专指现有历史诊断结果；B1 专指 M0 冻结的当前生产文档基线。候选版本分别与同轨 B1/新冻结 raw baseline 对比，不能把 raw 和 normalized 分数直接当 A/B。实际 cascade 与显式 Marker 是不同 `execution_profile`，不得混成一组。

production_document 导出器只能消费该 document 的可用规范内容，不能从 raw JSON、源图片或 gold 再抽取来补齐缺失。若旧文档没有 block/page/order 信息，必须报告 `unavailable`、覆盖缺口和诊断，不能虚构顺序/页码。无法成立的指标不比较 delta；M0 须在算法优化前冻结替代的绝对质量/覆盖门槛及必要保真 fixture，或把该验收列标为 blocked。不能事后把“新结构首次可测”说成相对旧不可测指标提高了多少。

**替代方案：**只继续优化基准脚本 raw Markdown 会掩盖产品丢失；把 raw 填回 normalized 评分会造成测量污染；全部改为 RAG 分数又无法定位解析问题，因此不采用。

### D2. 在 Research domain 内建立小而明确的有序结构

拟在 `F:/github/Agora-Hub/backend/research/domain/` 定义 `DocumentStructure`、`DocumentPage`、`DocumentBlock`、`InlineMathSpan`、`TableGrid`、`TableCell` 等强类型契约，具体拆文件按当前模块大小决定。它们不是另一份 ResearchDocument，也不复制现有 figure/equation/table content 的真值：通过 element IDs 和规范 artifact 引用连接现有实体。

最低字段：

- artifact：`schema_version`、`normalization_version`、`source_hash`、`parser_fingerprint`、`profile_fingerprint`、`content_checksum`。
- page：1-based `pdf_page_number`、可选 `printed_page_label`、page dimensions/rotation、来源 dataset page id（只在 eval manifest，非生产策略输入）。
- block：stable `block_id`、`parent_id`、`kind`、`source_order`、`reading_order`、`order_status`、`order_reason`、`element_ref`、`source_locator`；bbox 的坐标空间/单位/原点/旋转变换必须明确。
- 内容保留：区分 raw backend content ref、typed normalized content/ref；`inline_math` 表示 raw/normalized LaTeX 与正文 offset/邻接，offset 明确绑定对应 text 版本。
- table：cell 的行列索引、rowspan/colspan、header role、text/math segments、空值类型、source locator/可选 bbox；有顺序的 header paths 和完整 grid 是真值。

页码转换由 backend schema adapter 负责一次，明确识别 `page_idx`、`page_id`、路径与一基页字段，禁止先猜后 clamp。复用 `rect_to_pdf_points` 和现有 crop/locator helper，补充单位/rotation 验证；未知坐标保持 unknown，不用页中心或整页框冒充准确 cell bbox。

树输出使用 node ID/所有权决定哪些节点承载正文，既不把父节点汇总文本和全部 children 再写一遍，也不因两个不同位置的文本相同而去重。caption 必须保留为可寻址 block 或可靠关联的 caption，不能全局忽略后失去内容。

存储继续走现有 artifact persistence 和 document 引用；大 raw JSON、图片不嵌入每个 chunk。元素身份、source identity 保持稳定；解析结果缓存身份另含 schema/profile/normalizer/source 指纹。HTML/LaTeX 无 PDF 页码的内容保留已有合法 locator，不要求伪造 PDF geometry。

**替代方案：**只在任意 metadata 字典中塞字段难以强制验证；把全部文本和图片复制到每个实体/块成本高且会多真值；重构所有来源 parser 超出本次范围。

### D3. 保守、分区的阅读顺序恢复

先以 backend 给定顺序为候选；检测明显异常，如跨栏交错、页倒退、同一 block 重复、跨页 section bounds 错误。只在 bbox/页面类型可靠且 frozen policy 适用时生成几何候选：

1. 分页并校验坐标，标识标题、正文、表格、公式、caption、页眉脚注等角色。
2. 在页面 **纵向分区** 中识别栏与跨栏块；跨栏标题/表格构成相邻 band 的边界，而不是把所有跨栏块都挪到页首。
3. 在每个 band 内按已确认的书写方向/列顺序，再按列内位置排序；保留 inline 元素与所在文本、caption 与图表关系。
4. 使用稳定 tie-breaker 和显式 parent/child 约束构造合法序列；缺坐标、旋转/非适用布局、冲突边或低置信时保留原顺序并标记不确定。
5. sections 记录实际覆盖所有 block 的 `page_start/page_end` 和 per-block locators，不能把跨页 section 全部指向起始页。

顺序的语义正确性仍靠标注和错误分析验证；deterministic gate 只能验证结构/规则及适用条件，不能保证任意页面语义顺序。单栏 reading-order 的历史均值也较差，不能仅根据“双栏文本 ED 较高”断言顺序错误全部来自双栏。

**替代方案：**全页按 `(y,x)` 排序、按 x 直接二分、固定“左栏全部→右栏全部”会破坏混合布局，不能作为无条件策略。采用模型提出顺序也必须作为 candidate，不能让其直接控制 routing/gate。

### D4. 公式先保真，再做受控恢复

先修复转换链：按 backend 字段类型保留 LaTeX，不将纯 LaTeX 里的 `<`/`>` 当 HTML；解析 HTML math 节点时保护公式类型和边界；保持 inline 与 display 区分、原始标签/编号和上下文。对多行 align、matrix、上下标、分式、转义 `$`、公式中的文本做 fixture 和真实错误页回归。

renderer 校验只回答“此表达可在受限环境渲染”，不回答“与原文数学含义一致”。不做通用的语义改写、补符号或变换成看似等价公式。CDM 与 Edit_dist 用于离线评价，不参与在线 gold-aware route。

若证据显示 backend 真识别错，才评估已有获准 backend 对 ROI 的一次候选重解析或不同已支持识别 profile。不得默认开启外部收费/LLM 服务；配置须明确 opt-in、provider、允许数据、预算与失败策略。candidate 与原版并存，结构 gate 可拒绝明显无效候选；无法确定内容更可信时保持原输出并 degraded，不把“可渲染”当覆盖原结果的唯一依据。

**替代方案：**对全部页面全量重 OCR 成本高且会改坏正常页；直接让 LLM 补齐缺项会污染研究证据，均不作为默认。

### D5. 复杂表格的结构与内容分开诊断

用安全 HTML parser 或 backend 原生 cell topology 生成 TableGrid。处理 spans 时占位但不复制为多个独立内容 cell；明确区分空白、覆盖格、缺失、无法识别。重复表头保留原文字和各自列身份，内部稳定列 ID 不改写用户看见的 header。无表头和只有一行的表格也不能被静默清空。

`ResearchTable.rows` 如为既有消费必需，可由 grid 投影产生，声明 projection version 和不可表达项；实际 row-group/parent chunks 必须携带 header path、cell source mapping 和必要 formula 字段，不能把 lossy dict 重新定义为复杂表格真值。`visual_description` 只作独立辅助文本，不覆盖 cell values。

诊断分三层：结构合法性（span/row/col）；内容保真（raw→grid→chunk）；backend 内容识别正确性（offline gold）。TEDS_structure_only 与 TEDS 的差距是线索，不是 cell 错误率。cell-level 文本/数值一致性若作为项目诊断需单独命名、记录 normalization policy 和适用样本，不标成官方 TEDS。

只有在 cell topology 和 crop 有可靠来源时才允许 ROI 文本重识别；没有 cell bbox 不猜格子剪图。对原生 PDF 可选的文本层对齐必须先过现有 PyMuPDF admission；raster OmniDocBench 输入没有原生文本层，不能给它设置虚假的 text fallback。

**替代方案：**普通 Markdown pipe table 无法完整表达合并格；统一删空 cell 会移位；根据表格上下文自动纠正数值/单位存在事实风险，均不能作为真值保存策略。

### D6. 质量检查复用现有控制面，不另造 agent

扩展 `DocumentQualityProbe` 或其委派的纯函数，将检查拆成 typed rule results：`rule_id`、`status`、`severity`、`expected/actual`、受影响 page/block IDs 和可追踪 reason。输入是待解析文档及 backend/原始 PDF 可观测事实，不包括 benchmark gold。

- parse execution 成功、结构有效、内容完整/可信三类状态分开，不因 exit code 0 就称语义解析成功。
- hard failures（source identity 错、页码越界、unsafe artifact、grid 冲突等）不得正常入库；软问题保留原内容并按 policy degraded，交给既有输入/证据 gate 决定使用资格。
- 恢复通过既有 Harness `PLAN → EXECUTE → VERIFY` 和 execution adapter，传入显式 `max_replans`、`max_turns`、attempt、page/region、时间/资源预算；M0 冻结具体值。
- 默认只启用确定性转换修复；候选识别恢复初始建议最多每个 region 一次、每文档三处，仍受更严格的 Harness 总预算约束。超时/取消必须确认终止或记为 indeterminate，不能无上限 fallback。
- 每次 phase transition、candidate、采用/拒绝原因、profile 和 execution receipt 使用现有 durable events/artifacts；对缺能力、未知结果或预算不足 fail closed。
- LaTeX rendering 禁用 shell escape，限制输入/输出路径、网络、资源；HTML 保留表格/数学所需允许元素与属性但不执行 script/event handler；不把文档内容当 tool 指令。

`NEWSROOM_MARKER_DOCKER_ARGS` 是容器运行参数，不能拿来拼识别/恢复模型参数。新增识别选项通过现有 settings/composition 的 typed allowlist 承载，业务模块不读任意自由 argv，也不在文档内记录 secrets。

**替代方案：**LLM 质量裁判控制路由或直接覆写原解析、benchmark runner 自行 subprocess 绕过 production receipts、为此再造执行状态机，都违反现有架构边界。

### D7. 固定协议、分母及无泄漏数据边界

M0 建 manifest，至少包含：dataset revision、逐页 source/image hash、annotation version/hash、paper/version-family group、split/exposure、track、execution identity、repo commit **及 dirty patch/content digest**、parser image digest/weights/config、schema/normalizer/exporter/chunker/evaluator versions、match method/config、input mode、page mapping、预算、硬件/依赖、每次 attempt/receipt/output checksum。不可复原的历史字段写 unknown，不回填猜测。

gold 由独立 offline scorer 读取；生产解析 worker 仅获得文档内容、必要 source identity 和已许可配置，不挂载 gold/评分缓存/人工修正结果，不能按 benchmark 样本 ID 特判。评测 scope/filter/split 属于 evaluator，不传成生产解析决策特征。

215 页建为已暴露 dev/regression，按来源论文/版本族归组；源身份未知的页不作为无泄漏 document-heldout。正式 production 轨以真实文档为 parse 单位；无法还原原文档的页面作为独立单页 source 运行，不再把不同论文拼进一次语义文档解析。批处理只改变调度，不改变 source/document 边界；历史拼接 PDF 仅用于标识清楚的诊断复查。raw 和 production 正式对照必须使用相同输入模式/page mapping。

单页 raster 基准可评 parser 输出，但不代表完整论文 ingestion 的准入或端到端流程通过；现有 section-count/正文长度等 gate 的状态单独报告，不为跑分静默放松生产门槛。真实多页 native/raster PDF 另测；其评分协议和人工标注规则单独命名，不与 OmniDocBench 官方排名混淆。

所有 requested pages 保留状态、输出和缺失/失败原因。上游终态、official matching status、metric status 分开保存；缺类别标注为 not_applicable，evaluator 错误为 error，缺输出为 missing/failed，而不是统统记 0 或去掉。对存在 gold 的漏识别，沿用官方匹配/缺失处理并同时记录覆盖缺口；评分器无法打分时保留不可评状态，发布 completeness gate 不通过。

对指标同时保存官方 raw JSON、page aggregation、object/sample aggregation、分母、未匹配和有效集合。官方 normalized ED 按固定 evaluator 的 max-length/页内权重口径，不自改公式。汇总仅在协议/匹配/样本集合可比时给出 delta；若系统失败改变有效集合，还必须显示全请求集合的失败变化，不能只选双方成功样本宣传进步。

### D8. 错误样本、阶段对照与发布 gate

M0 人工抽检先从 dev 集按低分/随机/布局/语言抽样，目标至少覆盖公式、顺序、表格三类各 10 个有效错误实例和至少 10 个正常对照；可重叠的页保留类别标签。样本不足如实记录，不能制造错误或称统计充分。错误分类至少区分 backend 识别、page mapping、normalizer、serialization、chunk projection、evaluator match/annotation issue；每个结论绑定 source crop/raw/normalized/prediction/metric。

开发对照按阶段：固定 baseline、只改内容保真、增加顺序修复、增加公式修复、增加表格修复、完整候选。版本组合和资源 profile 固定，避免每轮同时换镜像/模型/配置而无法归因。可以复用已保存的 raw/document 进行离线再评分，但标明 replay，不能冒充 live production 执行。

正式验证先冻结候选及门槛，再对独立 document-heldout 跑 baseline/candidate，必要时按 source group 做 paired bootstrap；具体置信规则、重复次数、最小样本量和不足处理在 M0 预注册。默认以 95% 区间和方向化非退化阈值报告；样本太少/类别无标注则 blocked 或限定支持范围，不自动视为通过。置信区间不保证 public benchmark 无训练污染。

新增 `document_parsing_acceptance` 与既有 `cascade_acceptance` 并列，状态为 pass/fail/blocked，引用 acceptance profile 与报告 manifest。pass 要求 complete/comparable/frozen、独立保留集、生产轨指标和下游保真、资源预算以及现有架构检查均满足。未声明该协议的旧报告仍可阅读，但新增 gate 是 not_evaluated/blocked，不能继承旧 ready=true。报告不自动发布配置或 skill。

### D9. 版本迁移与灰度保持局部

默认不开启新 fidelity/recovery profile。先建立可读的版本标签和同 source 同 profile 幂等缓存身份，再在隔离 eval artifact/index namespace 生成候选。旧 PDF 文档缺新字段时是旧版本/能力未知，不通过猜测填入新 schema；继续通过明确的旧已验证 profile 展示历史结果，不作为新 profile 的成功产物。

启用新 profile 的 source 先有限重解析，再重建对应 chunks/索引版本，只有整组验收完成才切换当前 snapshot 引用。失败保留上次已验证快照，source changed、profile/schema 不兼容时不能无声复用。rollback 恢复完整 parse/chunk/index snapshot 组合而非只改 parser 名称；保留原因与前后身份。新增兼容仅限识别存量 artifact 版本和受控 reparse/read，不建设通用 legacy adapter 层。

## Risks / Trade-offs

- **[较大数据契约改动]** → 尽量复用实体和 persistence，新增有序结构以引用连接；先写迁移/回滚合同，再启用消费。
- **[后端识别错误无法靠 normalization 修复]** → 错误分类和 raw/normalized 轨分开；仅对真实 ROI 失败评估获准恢复，不承诺仅改代码就达成全部目标。
- **[高置信几何规则仍会错]** → 保留 source order、适用域、slice gates 和显式 profile，低置信不盲重排。
- **[表格投影损失影响既有 chunks]** → 保留 parent/row-group 机制，cell mapping 和完整性检查先行，逐步替换 lossy 真值使用处。
- **[固定阈值受 B1 天花板或低样本量影响]** → M0 预注册 feasible goals/最低样本量；B1 接近上界时用非退化和剩余错误下降门槛，不能看到候选后改阈值。
- **[渲染/重识别成本和安全风险]** → 默认不启用外部 provider，使用现有执行隔离和明确预算；增量 ROI 成本单列。
- **[并行 changes 修改相同模块]** → 实施前重新读取 backend removal/RAG protocol 状态；按职责集成，不覆盖未完成工作，冲突先协调。

## Migration Plan

1. 完成 M0 数据身份、失败账本、真实 B1 和目标冻结；基线不可比较时先停止宣传 delta。
2. 增加结构合同和 versioned serialization；用 fixture/真实 dev cases 证明 raw→domain→chunk 转换保真；不更改默认配置。
3. 在显式 profile 中逐项加入顺序、公式、表格修复；每项都跑对应 slice 和正常对照。
4. 完成独立真实多页 PDF 与两种业务 ingestion 的集成验证；freeze candidate 后执行 held-out 验收。
5. 由 owner 决定 opt-in 灰度；通过既有 artifact/index 切换与 deterministic gate 激活，不修改 active skills。
6. 未达门槛或出现来源/内容回归即回到完整已验证 snapshot/profile，记录原因；修复之后用新候选重新评估，不覆盖历史成绩。

## Open Questions

- M0：真实 PDF 文档组来源、许可和人工标注能力是否足够形成未暴露 held-out？当前 215 页不能满足该条件；未满足时允许开发迭代，不允许生产优化验收通过。
- M0：当前实际 Marker 镜像/模型 digest 和 schema 版本如何冻结？现有 `latest` 的历史身份不可凭空补齐。
- M0：旧 production document 有哪些指标可被忠实导出？不可评的维度须预注册覆盖/绝对门槛，而不是借 raw data 伪造 B1。
- M1：有序结构内联摘要加 artifact 引用的具体大小上限，需按现有 persistence/reader payload 约束确定；不能另建 store。
- M3：是否确需额外公式/表格识别能力？只有被验证的错误案例、资源预算和执行能力都具备才启用；否则保留现有 backend，诚实报告未达目标。

## Verification Strategy

- 普通无网络单元测试：嵌套 blocks、重复文本不同位置、页索引边界、旋转坐标、双栏跨栏、跨页 section、inline/display math、空/合并 cell、多层/重复表头、恶意 HTML/LaTeX、预算耗尽、缓存版本错配。
- production integration：真实 composition/execution receipt、持久化 round-trip、chunk content/cell locator mapping、单篇及跨来源 ingestion parity；测试 fake 仅用于边界测试，不能标成 production 分数。
- benchmark：固定 OmniDocBench checkout、config 和 manifest；两轨正式输出；失败/不可评不消失；完整 dev 回归 + 独立文档 held-out；性能同机同 cache 条件并区分各阶段成本。
- 实施阶段运行匹配范围 tests 以及 `python -m scripts.dev compile`、`python -m scripts.dev test`、`python -m scripts.dev smoke`；检查失败修根因，不跳过或削弱断言。
- 本规划阶段只执行 `openspec validate research-pdf-parsing-quality-optimization --strict` 和文档一致性检查，不冒称实现/性能测试通过。
