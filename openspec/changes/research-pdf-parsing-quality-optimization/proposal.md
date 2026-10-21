## Why

论文 PDF 里的公式、混合/双栏顺序和复杂表格，在 backend 输出、Research 文档归一化和 chunk 序列化之间可能发生不同类型的信息损失。现有 OmniDocBench academic-literature 215 页结果主要评的是 Marker 原始 JSON 转出的 Markdown，不能直接证明生产 `ResearchDocument` 及下游证据质量，需要先对齐基线和产品路径，再做可验证优化。

产品背景、已核验分数、目标和边界见 [PRD](F:/github/Agora-Hub/openspec/changes/research-pdf-parsing-quality-optimization/prd.md)。本次仅交付规划；以下均为后续实施范围。

## What Changes

- 在现有 Research domain/document 模块增加有版本的 page/block 顺序主干及 table cell-grid 契约；保留 raw artifact、公式内容、页码/bbox、父子关系、原始顺序、产品采用顺序与 source lineage，防止转换和下游消费中的静默丢失。
- 基于已复核错误样本，分别优化多栏/跨栏/跨页顺序、inline/display 公式恢复和复杂表格网格/文本；不盲目重排全部页面，不用视觉描述替代单元格，也不以公式可渲染代替内容正确。
- 扩展现有 deterministic parse quality/probe：显式检查结构完整性和来源定位，按冻结 policy 给出 parsed/degraded/quarantined；恢复仅使用已授权执行能力和有限预算，记录 durable attempts/receipts。
- 建立 `backend_raw` 与 `production_document` 两条评分轨，以及真实 `downstream_projection` 保真检查；区分历史 B0、冻结生产 B1 和候选结果，不让 normalized 轨偷读 raw 输出来补全产品缺失内容。
- 固定数据、evaluator、parser 镜像/config/schema 指纹、失败/缺失分母与统计口径；215 个已暴露页面只作为开发/回归材料，独立 document-heldout 另行冻结。复用既有 parser bake-off 报告和 RAG evaluation 边界，不建设平行评测平台。
- 增加 schema/cache 失效、受控 reparse、opt-in profile 和 rollback 方案；本变更不改默认后端或生产配置，不重新引入 MinerU。

## Capabilities

### New Capabilities

- `research-document-parse-fidelity`: 定义 page/block 顺序、公式、复杂表格和跨页 locator 的规范化保真、生产投影与版本身份。
- `research-document-parse-quality-control`: 定义确定性检查、降级/隔离、有界局部恢复、执行审计和不可信内容安全边界。
- `research-document-parsing-evaluation`: 定义分轨协议、基线冻结、无泄漏输入、完整分母、指标可比性、独立保留集及逐例证据。

### Modified Capabilities

- `parser-cascade-bakeoff-acceptance`: 新增文档解析协议身份、分轨结果和独立 document-parsing acceptance 检查；保留现有 penalized metrics / cascade acceptance，禁止把旧资格结果误当作新增生产解析质量门槛通过。

## Impact

- 主要实现 owner：`F:/github/Agora-Hub/backend/research/domain/document.py`、`F:/github/Agora-Hub/backend/research/document/marker_pdf_parser.py`、同目录的 `cascade_parser.py`、`chunker.py`、`chunk_manifest.py` 以及既有 artifact persistence ports。新增小型 domain/normalization 模块按职责落位，不向 domain 反向导入 adapter。
- 复用 `F:/github/Agora-Hub/backend/research/rag/evaluation/`、`F:/github/Agora-Hub/backend/research/application/`、`F:/github/Agora-Hub/interfaces/composition/` 和既有 Harness execution/transcript，不新增 store 或绕过 application service。
- OmniDocBench 作为隔离的可选离线评测依赖；不放进普通生产运行或无网络单元测试的必需路径，不把 dataset gold 提供给生产 parser。
- 与 `remove-mineru-pdf-backend`、`research-rag-evaluation-protocol`、`research-evidence-context-quality-foundation` 协调输入/输出契约；不修改其任务进度、不替代其职责、不重引 legacy Paper Radar 或旧 runtime 路径。
- 规范化 artifact schema 会版本化，受影响的 parse/chunk cache 显式失效和重算；现有 document/source identity 不被无故改写，不将旧 artifacts 伪装成新结构已验证产物。

## Non-goals

不更换默认模型/API、训练新 OCR、重建检索/重排算法、扩展研究报告 agent、改变 skill promotion、自动写 active skill 或自动发布。此次不实施优化，不运行新的付费实验；提案和目标不构成优化完成或成绩达标声明。
