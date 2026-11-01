# 代码整理 PRD

状态：待评审 · 日期：2026-10-30（复核版，初稿 2026-10-29）· 基线：`main` @ 696d593b（初稿基线 ca299c6f）

> 复核说明：初稿之后合入了模型连接（BYOK）第 1–8 步，以及 PR #3（工具稳定性测试与覆盖检查，47bc606c7）。实施每一步前都要按当前代码重新确认候选，PR #3 就已经让 `ToolBatchExecutor` 从“删除”变成了“保留”。本版重新统计了第 4 节，逐条核对了 5.1–5.6 的候选和拆分方案（每条结论都附了代码位置），新增 5.8 跨端一致性、5.9 仓库配置两节，第 10 节列出需要拍板的决定。

## 1. 背景

项目从 Agora Hub（新闻、项目、报告、工作室后台）演进到托管版论文工作台，期间经历了 graph-only 编排迁移、本地桌面运行时移除、瘦客户端改造等多轮重构。功能都还在，但代码里积累了几类问题：

- 一部分代码已经没有任何地方调用，或者只剩测试在调用。
- 评测代码（RAG 评测协议、解析对比、基准套件）和业务代码混在 `backend/research` 里，生产镜像也一起打包。
- 文件大小两极分化：有 36 个源文件超过 2000 行，最大的 7296 行；同时有 59 个 Python 模块不到 15 行，很多只是一个空壳类。
- 测试替身（`fake.py`）和前端假数据（`mock-data.ts`）放在生产代码目录里，前端有几个页面在接口没返回数据时直接显示假数据。

这些问题让新改动很难定位、评审很难看全、出错时影响面难判断。本次整理的目标是降低后期维护成本，不改变任何对外行为。

## 2. 已确认的决定

| 问题 | 决定 |
| --- | --- |
| 托管版用不到的旧功能（新闻与来源采集、项目、报告、工作室、门户首页、订阅、MCP、SDK、apps/web） | 全部保留，只删没有任何地方使用的死代码 |
| 评测代码 | 保留，挪到独立的评测包；生产代码不再依赖它，生产镜像不再打包 |
| 窗口准入的前后端分歧（2026-10-30） | 加后端预览接口，前端删掉自己的窗口算法（5.8 方案 A） |
| `/tech`、`/topics`、`/topics/[id]`、`/search` 四个只有假数据的页面（2026-10-30） | 删除，连同只服务它们的 hook、组件和假数据（5.5 第 3 类） |
| 第 4 步拆超大文件的范围（2026-10-30） | 全部超限文件（59 个 Python、7 个 TS）都在本次范围内；仍按“一次一个文件、单独提交”执行 |

## 3. 目标与非目标

目标：

1. 删除没有调用方的代码，以及只被自身测试调用、也不属于评测的代码。
2. 评测代码独立成包，依赖方向只能是评测依赖业务，不能反过来。
3. 把超大文件按职责拆开，把过碎的文件按职责合并，定下文件大小的规则。
4. 生产代码目录里不再有测试替身和假数据。
5. 把规则写成架构测试，放进 CI，防止整理完又长回去。

非目标：

- 不改 HTTP 接口、CLI 命令、MCP 工具、事件格式、数据库结构和存储文件格式。
- 不删功能，不改功能行为，不做性能优化。行为变化只有这几处，都单独提交：5.5 前端不再用假数据填充页面（含第 10 节第 2 项决定的结果）；5.8 修复思考预留缺陷；5.3 里事件存储两份实现统一异常类型。
- 不顺手重写算法或改命名风格；拆分文件时代码原样搬动，只调整导入。
- 不改桌面端（刚完成三种连接方式改造，体量正常）。

## 4. 现状数据

统计范围：git 跟踪的文件；“源文件”不含测试。复核版的统计脚本是 `.tmp/cleanup_stats.py`（实施步骤 0 会把它挪进 `scripts/`），前端用模块图脚本另算。

| 指标 | 初稿（ca299c6f） | 复核（696d593b） | 变化原因 |
| --- | --- | --- | --- |
| 源文件（py/ts/tsx/js/mjs） | 2434 个 | 2496 个，约 62 万行 | 模型连接新增约 60 个文件 |
| 源文件 ≥ 1000 / ≥ 2000 / ≥ 4000 行 | 113 / 36 / 8 个 | 115 / 36 / 8 个 | 新增 `openai_compatible.py`（1619）跨过 1000 行 |
| 超过上限：Python > 1500 行 / TS > 1000 行 | — | 59 / 7 个 | 新增指标，对应 5.3 的上限 |
| Python 非 `__init__` 模块 ≤ 15 行 | 59 个 | 59 个（`framework/memory` 16、`backend/research` 13） | 无变化 |
| 测试文件 ≥ 1000 / ≥ 2000 行 | 40 / 8 个 | 42 / 8 个 | `test_model_connections.py` 等 |
| `__init__.py` 总行数 | 16373 行，31 个懒加载表 | 16464 行，31 个懒加载表；`framework/harness/__init__.py` 1943 行 | 导出项小幅增加 |
| 没有任何引用的 Python 模块 | 13 个，456 行 | 脚本找到 12 个，400 行；人工复核后 11 个确认可删，`run_business_answer_eval.py` 是评测命令入口（见 5.1） | 静态分析找到的候选都逐个人工确认 |
| 没有任何引用的 Python 顶层类和函数 | 244 个，3118 行 | 62 个，713 行 | 口径收紧：只算名字在全仓库（含配置和文档）都不出现的。初稿的口径会把同名的不同符号算作没有引用 |
| 只被测试引用的 Python 模块 | 78 个，21942 行 | 78 个，23970 行；去掉评测后 60 个，12114 行 | 评测代码增加 |
| 只被测试引用的 Python 顶层符号 | 297 个，14838 行 | 71 个，2714 行 | 口径同上 |
| 只被脚本引用的 Python 模块 | — | 63 个，26749 行 | 新增指标，多数是评测 |
| 生产目录里的测试替身（按文件名） | 17 个文件，2793 行 | 17 个文件，2780 行 | 另有约 10 个替身类写在非 fake 文件里，见 5.5 |
| 前端没有任何页面用到的文件 | 58 个，3746 行 | 56 个，3328 行 | 按模块图重算并去掉 3 个误报（见 5.1） |
| 前端只被测试用到的文件 | 14 个，1302 行 | 14 个，1303 行 | 无变化 |
| 评测相关代码 | `rag/evaluation` 52 个文件 35027 行 | `rag/evaluation` 60 个文件 37168 行 | 期间新增 QASPER 与回答评测 |
## 5. 整理内容

### 5.1 删除死代码

判定标准：一个模块或顶层符号，从生产入口（`news` CLI、HTTP API、MCP、worker、Next 页面和路由、桌面端）、脚本和评测入口都走不到，并且 `configs/`、`backend/foundation/skills/**`、`sources.yaml` 这类配置文件里也没有按名字引用它。只被它自己的测试引用的代码同样算死代码，和测试一起删除。

静态分析会漏掉按字符串动态加载的代码（懒加载导出表、`importlib`、技能包注册），所以每个候选都要人工确认：全仓库搜索名字，再跑完整测试。

Python 候选（复核结论：用模块路径和全部顶层符号名搜全仓库，含字符串引用、懒加载表、`configs/`、技能包目录、`scripts/`、`data/`；backend 里没有 `importlib`/`import_module`；技能包 SKILL.md 只注册 `llm`、`schema_validator` 两个工具，不按模块名加载）：

| 模块 | 行数 | 结论 | 证据 |
| --- | --- | --- | --- |
| `backend/research/application/batch_ingest.py` | 106 | 删除 | `BatchIngestService` 等只在本文件出现，`application/__init__.py` 懒加载表里也没有 |
| `backend/research/rag/evaluation/paper_system_eval.py` | 105 | **不删，随 5.2 挪进评测包**（初稿判断错误） | `data/eval/run_system_eval.py:30` 导入 `SystemEvaluator`、`SystemSample` |
| `backend/research/rag/cli/run_business_answer_eval.py`（初稿漏列） | 54 | 随 5.2 挪进 `evaluation/cli/` | 它是 `run_business_answer_eval` 唯一的命令入口，默认数据 `data/eval/business_question_bank_v1_draft.jsonl` 存在，但 `scripts/dev.py` 和 CI 都没调用它 |
| `backend/layers/output/output_quality_checks.py` | 52 | 删除 | `check_card_output_quality`、`check_report_output_quality` 全仓库零命中 |
| `infrastructure/external/source_adapters.py` | 52 | 删除 | 全仓库搜 `source_adapters` 零命中；同名函数在 `infrastructure/external/sources/__init__.py:65,73` 还有一份，`infrastructure/external/__init__.py:3` 导入的是那份 |
| `backend/layers/signal/quality_checks.py`、`backend/layers/extraction/quality_checks.py` | 62 | 删除 | `check_signal_quality`、`check_extraction_quality` 零命中 |
| `backend/layers/signal/deduplicator.py` | 17 | 删除 | `deduplicate_signals` 零命中。SKILL.md 里的 “deduplicators” 是普通英文描述，`event-deduplication` 技能的兜底实现是 `foundation/skills/fallbacks.py:52`，不是这个模块 |
| `backend/research/rag/cli/run_parser_bakeoff_ingest.py`、`run_parser_url_bakeoff_ingest.py` | 15 | 删除 | 只转发到评测模块，被转发的两个模块自己有 `__main__` 和 `prog=` |
| `backend/research/memory/`（含 `reader_repair_memory.py`） | 25 | 删除整个目录 | `ReaderRepairMemoryRepository` 只在 `research/memory/__init__.py:3` 出现；名字相近的 `PostgresReaderRepairMemoryRepository`（`interfaces/services/reader_repair_factory.py:24`）是另一个类，和 `graphs/reader_repair*` 也无关 |
| `backend/layers/analysis/ranking_features.py` | 7 | 删除 | `ranking_features_from_score` 零命中；别处的 `ranking_features` 是同名字段（`foundation/models/board.py:47`） |
| `backend/layers/relation/relation_quality_checks.py` | 15 | 随 relation 目录删除 | `check_relation_quality` 零命中 |

没有引用的大块符号（复核结论）：

| 符号 | 结论 | 证据 |
| --- | --- | --- |
| `framework/tool/inspection/testing.py`（`ToolTestRunner`、`ToolTestCase`、`ToolTestReport`） | 删除整个文件 | 只出现在 `tool/inspection/__init__.py:25,45-47`、`tool/__init__.py:63-65,222-224` 的导出；`tests/` 无引用。同步删 `tests/architecture/test_harness_execution_and_child_caller_boundaries.py:20,28` 的白名单项 |
| `harness_decision_input_snapshot`（`control_plane/replay_history.py:141`） | 删除 | 搜调用 `harness_decision_input_snapshot\(` 只命中定义；其余是 `control_plane/__init__.py`、`harness/__init__.py` 的导出 |
| `LazyExports`（`framework/shared/lazy_exports.py`） | **保留**（初稿判断错误） | 21 个 `__init__.py` 在用，例如 `framework/harness/__init__.py:5,633`、`interfaces/composition/__init__.py:5,44`；5.6 收缩后层入口还要用 |
| `AnalysisPipeline`、`GoldenSetBuilder` | 删除（同文件的其他符号保留） | 见下方“整块只剩测试在用的目录”后的 backend 表 |

只被测试使用的非评测代码，逐个判断：

- 生产里已有正式实现、这个只是给测试用的内存版或简化版：挪到 `tests/support/`。
- 给可选部署留的适配器（Redis、Qdrant、Postgres 相关）：保留，但必须有一个装配入口按配置走到它，并在模块文档里写明由哪个配置启用。没有装配入口的按下一条处理。
- 既不是上面两种、测试只是在测它自己：连同测试一起删除。

整块只剩测试在用的目录：`backend/layers/relation/`（17 个文件 2033 行）和 `backend/layers/analysis/quality/`（8 个文件 2481 行），合计 4514 行。复核结论：删除成立，但初稿写的理由不对——`AnalysisPipeline` 根本不导入这两个目录（`analysis/pipeline.py:1-29` 只导入 `backend.foundation` 和 `layers.extraction.models`），它们在生产代码里**没有任何导入方**，只有两个目录互相引用（`quality/support_matrix.py:7`、`citation_checker.py:11` 导入 `relation.evidence`）。`backend/layers/analysis/tools.py:8` 用的是 `quality_records`，不受影响。处理方式：

- 连同 `tests/backend/layers/relation/**`、`tests/backend/layers/analysis/quality/**`（5 个文件）删除，并从 `scripts/dev.py:416-418` 去掉对应条目。
- 先改后删：`tests/backend/layers/test_helper_decoupling.py` 同时测了 relation 和其他层；`tests/infrastructure/storage/{test_lineage_store,test_persistence_records}.py` 用 relation、quality 的类型构造测试数据，测的是 infrastructure 的存储，改成用 `quality_records` 或 `infrastructure/storage/lineage/evidence.py` 自己的类型，不整文件删。
- `analysis/pipeline.py` **不能整个删**：`AnalysisPipeline`（约 300 行）没有调用方可以删，但同文件的 `AnalysisResult` 被 `layers/output/` 四个模块导入，`TechnologyRadarItem` 被 `output/models.py:19`、`output/report_builder.py:16` 导入，`trend/quality/maturity/impact_analyzer.py` 等用 `from backend.layers.analysis import pipeline as rules` 引用其中的规则函数。

**复核新发现：旧板块流水线整条可能都是死代码。** `layers/output` 的 `BoardOutputPipeline`、`BoardCardBuilder`、`InsightBuilder`、`DetailPageBuilder`、`ReportBuilder` 在 `layers/output` 以外只被 `tests/backend/layers/test_output_layer_hardening.py` 引用；`ExtractionPipeline`、`SignalPipeline`、`TrendAnalyzer`、`TechnologyRadarAnalyzer` 在测试以外也只在各自层内出现。output 层在生产里真正被用的是 `worker_handlers`（`interfaces/services/worker_service.py:12`）、`tools`/`postgres_tools`（`backend/tools.py:7-8`）、`report_quality_projection`（`interfaces/services/report_service.py:8`）。signal → extraction → relation → analysis → output 这条流水线在实施步骤 1 里单列一项逐个复核；signal 层的 `SourceArtifactPublicationService`、`SourceHealthCheckTaskHandler` 等要单独确认是否有 worker 在用。

backend 下需要归类的较大符号：

| 符号 | 结论 | 证据 |
| --- | --- | --- |
| `AnalysisPipeline` | 删除，同文件的类型保留 | 见上 |
| `GoldenSetBuilder`（`rag/evaluation/paper_gold_builder.py:105`） | 删除，同文件的 `load_golden_set`、`QAPair` 保留 | 只出现在 `evaluation/__init__.py` 懒加载表；`data/eval/run_*_eval.py` 只用 `load_golden_set`，`paper_replay.py:6` 只用 `QAPair` |
| `ProtocolBudgetLedger`（`rag/evaluation/protocol_budget.py`，整个模块 1255 行） | 随 5.2 挪进评测包；**是否整模块删除需要你拍板** | 模块里所有导出名在测试以外零引用，没有任何评测入口用到它 |
| `BusinessSkillRuntime`（`foundation/skills/runtime.py`，273 行）及 `fallbacks.py`（214 行） | 删除，连同 `tests/backend/foundation/test_business_skill_runtime.py` | 测试以外零引用。技能包目录（SKILL.md、schemas、prompts）由 `tests/backend/foundation/skills/*` 校验，和 runtime 无关，保留 |
| `PdfSnapshotMigrationService`（`research/services/pdf_snapshot_migration.py`，247 行） | 删除，连同测试 | 测试以外零引用；模块文档自己写明“拒绝 promotion 和 rollback，直到存储能力存在”，是半成品 |
| `InMemoryResearchCatalogRepository`（`application/catalog.py:1169-1380`） | 挪到 `tests/support/`，删掉 `application/__init__.py:19,91,170` 的导出 | 生产实现是 `infrastructure/research/catalog_store.py:55` 的 `FilesystemResearchCatalogStore`；只有 4 个测试文件使用 |
| `ScoringRuntime`、`TaskPlanRecoveryService` | 归属更正：在 `framework/`，不在 backend；实施时按同样方法复核 | — |

需要逐个归类的较大符号（复核结论；backend 下的几个见下方 backend 部分）：

| 符号 | 结论 | 证据 |
| --- | --- | --- |
| `PostgresConversationStore`（`infrastructure/storage/postgres/conversation.py`，868 行） | 删除，或补装配入口 | 只有 `storage/postgres/__init__.py:11,28` 的懒加载导出和它自己的测试；生产只用 `LocalJsonConversationStore`（`composition/research.py:2275`）。迁移 `001_initial.sql`、`013_*.sql` 建的表按“迁移只增不改”保留，在 docs 写明 |
| `InactiveGraphStorageIndexAdapter`（`infrastructure/storage/indexing/inactive.py`，348 行）及 `LocalGraphIndexCandidateStore` | 删除，连同两个测试；`tests/architecture/test_graph_storage_index_boundary.py` 相应删减 | 类文档写明 “deliberately absent from live composition”（inactive.py:30）；indexing 包外没有生产引用。**如果 Gate B 还在计划里，改为保留并在模块文档写明由哪个门禁启用——需要你拍板** |
| `infrastructure/storage/redis_runtime.py`（217 行，初稿漏列） | 删除，或补装配入口 | `RedisRuntimeStore`、`InMemoryRuntimeStore`、`redis_runtime_store_from_env` 生产无调用方，只有 `tests/infrastructure/storage/test_redis_runtime_store.py` |
| `ToolBatchExecutor`（`framework/tool/runtime/batch_executor.py`） | **保留**（复核后合入的 PR #3 已把它接进生产） | `framework/agent/loop/loop.py:93,150,1059` 用它执行一批工具调用（提交 2baadfcea） |
| `InMemoryHarnessSignalInbox`、`HarnessSignalInboxPort`（`framework/harness/waits/`） | 删除，连同测试 | 文档写明只给测试用、生产要换持久实现（inbox.py:21-25），但不存在持久实现，生产也不消费这个端口 |
| `ContextCompactionReplayReader`（`framework/harness/runtime/context_replay.py:50`） | 删除，连同测试用例 | 是只读校验器而非替身，不挪 `tests/support`；没有生产入口 |
| `FakeSkillEvolutionPort`（`framework/harness/skills/evolution/fake.py`） | 删除，连同测试 | `SkillEvolutionPort` 没有生产实现。整个 `skills/evolution` 子系统是否都只剩测试在用，实施时另行核实 |
| `MCPToolAdapter` | 归属更正：在 `framework/tool/runtime/mcp_adapter.py:71`，不在 infrastructure | 只有 `framework/tool/__init__.py`、`tool/runtime/__init__.py` 导出和 4 个测试；按“没有装配入口”处理，删除 |
| `RagasJudgeAdapter`（`infrastructure/research/ragas_judge.py`，987 行） | 随 5.2 处理：接进 `rag_eval` 的装配，或删除 | 在 backend、interfaces、infrastructure、scripts、data 下都没有导入方，唯一引用是 `tests/optional/test_ragas_judge_compatibility.py:29`，即现在没有任何评测入口 |

前端（复核时按模块图重算：入口是 app-router 约定文件、`src/proxy.ts`、配置文件和 `scripts/*`，跟随 `import`、`export … from`、`import()`、`vi.mock`；在初稿基线上跑同一脚本，结果逐行一致）：

- **没有页面用到的文件：56 个，3328 行。** 已去掉 3 个误报：`deployment-config.d.mts`（类型声明，TypeScript 自动读取）、`public/theme-init.js`（`app/layout.tsx:29` 用 `<script>` 加载）、`public/pdf.worker.min.mjs`（PDF 组件设为 `workerSrc`）。另外 `types/katex.d.ts` 保留（依赖里没有 `@types/katex`）。按目录：
  - `features/admin/components/*`（4 个，1038 行，含 `admin-console.tsx` 867 行）、`features/admin/types.ts`（128 行）
  - `features/studio/runs/{components,hooks,lib/agent-run-adapter.ts}`（12 个，457 行）、`features/studio/artifacts/components/*`（4 个，140 行）、`features/studio/{components/studio-overview-dashboard,hooks/use-studio-overview}`（2 个，164 行）、`features/studio/shared/components/{studio-page-state,studio-section-card}`（2 个，100 行）
  - `features/memory/components/*`（7 个，214 行）、`features/quality/components/*`（4 个，136 行）、`features/topics/{components/topic-filter-panel,hooks/use-topic-evidence,hooks/use-topic-timeline}`（3 个，40 行）
  - 初稿漏列：`features/community/components/{community-filter-panel,community-source-list,community-topic-card}`（3 个，303 行）、`features/portal/evidence-graph-page.tsx`（135 行，页面实际用 `features/evidence-graph/evidence-graph-page`）、`app/studio/{artifacts,quality}/*-page-client.tsx`（2 个，40 行）、`components/common/placeholder-page.tsx`、`components/papers/{paper-metrics,papers-header}.tsx`、`lib/fonts.ts`、`lib/papers/{mock-data,taxonomy-source}.ts`、`stores/filter-store.ts`
  - `components/layout/{sidebar,topbar}.tsx`（203 行）、`components/ui/{select,separator}.tsx`（109 行）
- **只被测试用到的文件：14 个，1303 行**，连同测试删除。其中 `root-env.ts` 按本节末尾处理；`paper-reader-ops-panel.tsx` 是 594 行。
- `components/papers/__tests__/paper-pdf-viewer.layout.browser.mjs`：手动运行的浏览器脚本（第 42 行写明 `node …` 运行），没有 npm script 或 CI 调用它。**要么加进 CI，要么删除——需要你拍板。**
- `src/test/fixtures/` 里 6 个文件有 5 个（248 行）没有任何测试使用，删除。
- `features/admin/lib/{mock-data,i18n}.ts`（789 行）**没有被 git 跟踪**，原因见 5.9。导入它们的组件本身是死代码，一起删除；初稿“改名为 `navigation.ts`”作废。

其他：

- `scripts/graph_only_migration/`（9 个文件 5185 行）是 2026-08-25 归档的 graph-only 迁移工具，复核后分三部分：
  - 迁移本身（`reader` 557 行、`planner` 484 行、`transformer` 1482 行、`contracts` 1088 行、`__init__`），只被 `tests/scripts/test_graph_only_migration.py` 导入。运行时不读迁移前的格式：测试和脚本以外搜 `legacy_flat|legacy_checkpoint|LegacyRecordKind|BoundedLegacySourceReader|GraphHistoryTransformer|run_mapping\.json` 零命中。**还有没有环境保存着迁移前的数据，代码里查不出来，需要你确认**；确认没有就连同测试删除，在 `docs/` 留一段说明，需要时从 git 历史找回。
  - 被架构测试使用的扫描工具是 3 个（初稿写 4 个）：`import_graph`、`zero_reference_scan`、`deletion_proof`，挪到 `tests/architecture/support/`，继续生效。挪动时同步改 `test_graph_only_migration_boundary.py:10,63,74,107,114`、`test_harness_graph_namespace_boundary.py:61`、`deletion_proof.py:81-82,311,428` 的允许清单、`import_graph.py:20` 的 `HISTORY_PREFIX`、`_graph_only_freeze.py:27-28` 的 `NON_RUNTIME_AUDIT_FILES`，`graph_only_freeze_baseline.json` 用生成脚本重新生成。
  - `stale_reference_audit.py`（233 行）没有任何测试导入，只有自己的 `main`。**删除还是作为手动审计工具保留，需要你拍板。**
- 根目录的 `codex-handoff-*.md` 实际是 4 份（初稿写 2 份），都被 git 跟踪：`business-question-bank-equivalence`、`paper-rag-answer-outcome-integrity`、`pdf-parsing-quality-optimization`、`qasper-retrieval-evaluation-optimization`。移到 `docs/plans/archive/`（目录还不存在）；`docs/qasper-retrieval-evaluation-optimization-prd.md:181` 引用了其中一份，链接要跟着改。注意 `docs/` 被 `.gitignore` 忽略，挪过去等于从 git 移除，先确认这几份是否还要留在仓库里。

### 5.2 评测代码独立成包

现状：评测代码分散在五个层里，靠 `backend/research/rag/__init__.py` 的懒加载导出表把评测类暴露成业务包的公开接口。生产代码真正依赖的只有这张导出表，以及 `parsing_evaluation_summary.py` 这类只被评测 CLI 使用的模块。

新建顶层包 `evaluation/`，内部按评测对象分子包：

```text
evaluation/
  metrics/       通用检索与回答指标（原 framework/rag/evaluation）
  protocol/      评测协议：数据集、语料、执行、打分、预算、质量门禁（原 protocol_*）
  qasper/        QASPER 基线、实验与诊断（原 qasper_*、application/qasper_*）
  paper/         论文证据、回答、生成评测与报告（原 paper_*、ci_eval_gate、live_answer_*）
  parsing/       PDF 解析评测、解析器对比与验收（原 document_parsing_*、*bakeoff*、omnidocbench_adapter）
  reportbench/   ReportBench（原 backend/research/benchmark/reportbench.py、application/reportbench.py）
  ports.py       评测专用端口（原 research/ports/evaluation_*、qasper_evaluation）
  adapters/      外部实现（原 infrastructure/research/{ragas_judge,evaluation_dataset,evaluation_sources,qasper_evaluation}.py）
  composition.py 装配（原 interfaces/composition/evaluation.py、qasper_evaluation.py）
  cli/           命令入口（原 interfaces/cli/commands/{rag_eval,qasper_eval,parsing_eval,parsing_acceptance,reportbench}.py 与 backend/research/rag/cli）
```

规则：

- 依赖方向只能是 `evaluation` → `framework`/`backend`/`infrastructure`/`interfaces`，反方向一律禁止。`backend/research/rag/__init__.py` 里指向评测的导出项全部删掉。
- 留在业务包里的是业务概念，不搬：`backend/research/benchmark/{models,gates}.py`（论文里的基准分数，目录和流水线在用）、`backend/memory/evaluation.py`（记忆整合与策略学习在用）、`framework/llm/structured_output/evaluation.py`（AgentLoop 的结构化输出校验）。
- `backend/research/rag/visual/` 和 `backend/research/document/pdf_visual_sidecar.py` 现在只有脚本在用，先确认是不是评测专用：是就挪进 `evaluation/parsing/`，不是就补上生产入口或删除。
- `data/eval/*.py`、`scripts/research_pdf_parsing_eval.py`、`scripts/structured_output_eval.py` 改为调用 `evaluation.cli`；数据文件留在 `data/eval/`。
- 测试跟着搬到 `tests/evaluation/`。
- `pyproject.toml` 的包列表加上 `evaluation*`，本地 `pip install -e .[dev]` 照常可用。生产镜像只 COPY 四个业务包，自然不包含评测代码，`.dockerignore` 再显式排除一次。
- `python -m scripts.dev` 的 `rag-eval`、`reportbench`、`test-rag-eval-gate`、`test-rag-eval-contracts`、`check-live-answer-readiness`、`run-live-answer-eval` 命令名不变，只改内部路径，CI 和定时评测工作流不用改。
- `backend/research/rag/evaluation/paper_benchmark_suite.py`（3793 行）在搬进来时按 5.3 的规则一起拆分。

复核补充：

- 初稿列出的 interfaces、infrastructure 评测文件都存在，**没有任何非评测生产代码导入它们**：`composition/evaluation.py`（34 行）、`composition/qasper_evaluation.py`（527 行）、`cli/commands/{rag_eval,qasper_eval,reportbench,parsing_eval,parsing_acceptance}.py`、`infrastructure/research/{ragas_judge,evaluation_dataset,evaluation_sources,qasper_evaluation}.py`。
- 初稿漏列 `interfaces/composition/qasper_retrieval_replay.py`（134 行，只被 `qasper_eval.py` 和测试使用），一起搬。
- `rag_eval.py`、`reportbench.py` 只由 `scripts/dev.py:182-186` 注册，`news` CLI 里没有它们；`parsing_eval.py`、`parsing_acceptance.py` 没有导入方，只能 `python -m` 运行。搬包后统一走 `evaluation.cli`。
- **反向依赖（初稿没提）**：生产装配 `composition/research.py` 里嵌着评测专用的部件——L2024 的 `production_evaluation_parse_use_case`、L2388 的 `ProductionParsingGraphApplicationService`、`ResearchRuntimeComposition.production_parsing_evaluation_service`（L903），唯一使用方是 `scripts/research_pdf_parsing_eval.py:67`。搬包时改成由 `evaluation/composition.py` 用 composition 的公开部件自己装配，生产装配里删掉这三处。
- `RagasJudgeAdapter` 现在没有评测入口，见 5.1。
- backend 部分：`rag/evaluation/` 现在是 60 个文件 37167 行。评测包以外共 34 个文件、99 处导入它，**全部只能从评测入口走到**（`news` CLI 的 `COMMAND_MODULES` 里没有任何评测命令）。搬包前要切断或一起挪走：
  - `backend/research/rag/__init__.py` 的懒加载表：22 项里 19 项指向评测。全仓库 `from backend.research.rag import` 只导入另外 3 个名字（`ResearchRAGContext`、`ResearchRAGGapReport`、`ResearchRetrievalGoal`），19 个评测项可以直接删。
  - 一起挪走：`rag/adapters/evaluation_scorecard_adapter.py`（和评测包互相导入，是双向依赖）、`ports/evaluation_judge.py`（评测端口，进 `evaluation/ports.py`）、`application/` 下的 `evaluation.py`、`parsing_evaluation_summary.py` 和 9 个 `qasper_*.py`、`rag/cli/` 的 11 个文件。
  - `backend/research/benchmark/__init__.py:29` 会在导入时立即加载 `reportbench`，挪走 reportbench 时要同时改这个文件，否则 `benchmark.models` 的使用方会断。
  - `rag/visual/`（3 个文件 293 行）和 `document/pdf_visual_sidecar.py`（114 行）是评测专用（初稿说“只有脚本在用”不对，scripts 里一处都没有），挪到 `evaluation/parsing/`。生产的视觉描述走 `application/visual_chunk_describer.py` → `interfaces/services/paper_rag_factory.py:37`，不经过它们。
### 5.3 拆分超大文件

大小规则（源文件）：

| 级别 | Python | TS/TSX | 处理 |
| --- | --- | --- | --- |
| 目标 | ≤ 800 行 | ≤ 500 行 | 新文件按这个写 |
| 上限 | 1500 行 | 1000 行 | 超过即 CI 失败（允许清单见 6 节） |
| 单个类 | ≤ 600 行 | 单个组件 ≤ 300 行 | 超过要拆出协作对象或子组件 |

测试文件上限 2000 行，按被测行为拆分，共用的构造放进同目录 `conftest.py` 或 `tests/support/`。

拆分方法：

1. 先列出文件里的职责分组（看公开方法、方法名前缀、共用的私有状态），每组成为一个模块。
2. 原文件变成同名包，`__init__.py` 只重新导出原来的公开名字，调用方的导入路径不变。
3. 一个类过大时，按职责拆成几个协作对象，原类保留为门面，公开方法签名不变，内部转交给协作对象；协作对象之间通过构造参数传入共享状态，不互相引用私有成员。不用多重继承的 mixin 来“拆”文件。
4. 代码原样搬动，拆分提交里不夹带逻辑修改，评审时只需核对移动是否完整。

重点文件的拆分方案（复核版：每个文件都读了代码，统计了主类的方法数、方法名前缀和各组用到的私有属性；“初稿”一列写明初稿方案是否成立）：

| 文件 | 行数 | 初稿 | 拆分方向（按代码实际支持的分组） |
| --- | --- | --- | --- |
| `framework/harness/control_plane/harness.py` | 7296 | 不成立 | `HarnessControlPlane` 5782 行、114 个方法、26 个公开（保留为门面）。初稿六组只覆盖公开方法，私有方法里最大的三块没有着落。代码支持八个协作对象：运行准备与绑定注册表（持有 `_prepared_graphs`、`_prepared_run_specs`，几乎所有组都要用，作为构造参数传给其他协作对象）；活动执行输入与结果记录（约 330 行）；决策与观测应用，含等待（约 650 行，“等待原因”只有 2 个方法，不单独成组）；驱动循环与派发（约 400 行）；重放、水合、恢复与历史（约 750 行）；副作用（约 1500 行，再拆成 worker 副作用、终态副作用、fenced attempt）；gate（约 450 行）；输出投影与步骤输入（约 720 行） |
| `framework/harness/control_plane/graph_application.py` | 5839 | 部分成立 | 主要是 37 个大模块函数。步骤决策带上 `_activate_node`、`_apply_prepare_side_effect`（约 710 行）；运行决策和 control、merge 决策合一组（约 520 行）；初稿漏了观测应用（`_apply_wait_cause_observation` 等，约 640 行）、并行汇合与循环（L4139-4770，约 630 行）、`HarnessGraphControlPlaneRuntime` 与端口（约 1150 行） |
| `infrastructure/storage/events/postgres.py`、`sqlite.py` | 5725、4467 | 成立，共用部分更多 | Store 类 39 个同名方法之外，UnitOfWork 另有 9 个同名方法，模块级同名私有函数 25 个（其中 6 个 AST 完全相同，例如 `_subscription_definition`、`_redelivery_request_identity`），逐字相同的长报错 55 条。两份已出现分歧：`begin_redelivery` 的入参检查在 postgres.py:1790 抛 `ValueError`、在 sqlite.py:2048 抛 `TypeError`。分组：事件流、订阅、投递与租约、死信与重投、隔离区、重放报告、检查点、**事务状态（初稿漏列）**、行映射、游标编解码；SQLite 的备份与 WAL 单独成模块。共用模块先收 6 个相同函数和 `begin_redelivery`/`register_subscription` 里与 SQL 无关的校验，并统一异常类型（异常类型统一属于行为修改，单独提交） |
| `framework/harness/task_plan/parallel.py` | 4644 | 部分成立 | `ParallelAgentCoordinator` 2973 行、42 个方法、19 个公开。初稿五组之外还要：值对象与端口单独成模型模块（L90-1333，约 1250 行）；并行度策略改成纯函数（只读 `_allow_test_executor`）；会话账本（`_record_attempt`、`_mark_indeterminate`、`_emit` 等，连同 `_sessions`、`_lock`）做成共用协作对象。派发与波次执行（`_run_wave` 405 行）必须分开，否则合起来超过 1000 行 |
| `framework/harness/task_plan/replay.py` | 3953 | 有前提地成立 | `TaskPlanReplayReducer.replay` 一个方法 727 行，内部是约 20 个 `event_type` 分支和约 25 个可变局部变量。要按事件类别拆 apply 函数，先把这些局部变量收进一个 `_ReplayState` 对象。模块函数分四组：报告模型、输入校验（约 450 行）、并行事件应用（约 1490 行，再拆成“事件应用”和“spawn/恢复审计与容量结算”）、结果与终态应用（约 680 行） |
| `framework/harness/task_plan/stage.py`（初稿漏列） | 2627 | — | `TaskPlanStageRunner` 2440 行、47 个方法、4 个公开，没有私有字段，依赖都是公开属性。按依赖分：规划（约 450 行）、串行执行（约 340 行）、并行执行（约 560 行）、并行事件记录与容量证据（约 365 行，其中 3 个方法不访问 `self`，改为模块函数）、结果恢复（约 235 行）、调用（约 95 行）、持久化与停止；`run` 和 `_execute_plan` 留在门面 |
| `framework/harness/task_plan/store.py` | 3851 | 部分成立，初稿归属有误 | 模块级转移规则（约 1500 行）不属于内存实现：`durable_store.py` 从这里导入了 28 个私有函数，`replay.py` 导入了 3 个。拆成两个存储共用、去掉下划线的模块：capacity/wave/queue 契约校验（约 730 行）、事件构造（约 190 行）、plan 与 patch 规则。记录模型（L74-974）单独成模块。`InMemoryTaskPlanStore`（1255 行）按私有属性再分：候选与提交、计划与补丁、结果、事件提交、容量 |
| `framework/harness/task_plan/durable_store.py` | 3427 | 初稿没给分组 | `DurableTaskPlanStore` 2604 行、51 个方法、32 个公开（留作门面）。按私有属性边界拆：文档存取（只有这组用 `_artifact_store`，约 380 行）、事件流（只有这组用 `_runtime`、`_reader`、`_producer`、`_clock`，约 360 行）、gate 与 worker 输入产物（约 400 行）、父执行上下文（约 290 行）。转移规则用 store 拆出的共用模块 |
| `framework/harness/control_plane/durable_events.py` | 4380 | 部分成立 | 初稿四个类约 1700 行；另有约 2700 行模块函数要归位：commit 编解码（约 450 行）、投影校验/重放/恢复（约 650 行）、事件 id 推导（约 90 行）、phase 转移校验（约 240 行）、canonical payload 投影（约 330 行，和 `HarnessEventCanonicalAdapter` 放一起） |
| `framework/harness/control_plane/graph_evaluator.py` | 4134 | 部分成立 | `HarnessGraphEvaluator` 1200 行、只有 `evaluate` 公开，所有方法都不访问 `self` 字段，按候选类型拆成无状态模块。初稿漏了模型（约 490 行）、观测载荷校验（约 400 行）、wait 投影（约 345 行） |
| `framework/llm/routing/router.py` | 4350 | 部分成立 | 初稿漏了主问题：`_complete`（871 行）和 `_stream`（1439 行）占 2310 行，`_complete` 的 861 个非空行里 656 行和 `_stream` 相同（difflib 实测）。先把两者共用的尝试流水线抽成一个协作对象（候选部署 → context/cache/budget 准备 → provider 调用 → 结算与记录），再拆预算、缓存、结构化输出、事件这几组 helper（约 1100 行）。**这一步去重属于逻辑重组，不是原样搬动，需要单独提交、单独评审** |
| `framework/events/schema/catalog.py` | 3339 | 基本成立 | `EventSchemaCatalog` 只有 360 行，其余是按事件族写的 schema 函数：runtime、budget、task_plan（约 790 行，其中 `_parallel_task_plan_details_schema` 391 行单独成模块）、harness graph、attempt、harness、context_compaction、sensitivity。初稿漏了事件类型常量（L43-150，成 `event_types` 模块）和纯函数审计（L3074-3339，约 265 行，成 purity 模块） |
| `framework/llm/clients/openai_compatible.py`（初稿漏列） | 1619 | — | 超过 1500 行上限。改成包，`__init__` 重新导出 6 个公开名字：错误类型（`LLMProviderError` 等，`anthropic_messages.py:20`、`routing/router.py:33`、`routing/cooldown.py:8` 现在都从 openai 模块导入）和重试/传输（`LLMRetryPolicy`、`Transport`、`_urlopen_*`）提到 clients 层共用模块；其余拆成诊断与错误分类（约 335 行）、流式组装（约 190 行）、结构化输出（约 230 行）、响应规范化（约 200 行），客户端本体约 500 行 |
| `framework/llm/clients/config.py` | 1233 | — | 低于上限，只做 models.yaml 解析，最大函数 101 行，不拆。要压到 800 行目标时，把 schema 校验（约 330 行）拆成 `config_schema.py` |
| `backend/research/application/single_paper_runtime.py` | 3769 | 成立，要细化 | `ResearchSinglePaperRuntime` 1794 行，只有 1 个公开方法 `run`，加 22 个私有方法（`_run_bound` 一个就 365 行）。拆成：运行编排（`run`、`_run_bound`、恢复与投影暂存）；工件发布与载荷（`_publish_artifacts` 155 行等）；各阶段处理器（`_load_paper_source`、`_compile_document`、`_run_research_rag`、`_analyze_*` 等，每个只用 1–3 个协作对象，最容易拆成独立的阶段处理器）；结果模型 `ResearchAnalysisResult`（338 行）与持久化校验函数；转录与投影函数（L3088-3740）。初稿说的“兼容处理器”其实是测试专用实现，按 5.5 挪走，不是拆文件 |
| `backend/research/application/parse_paper.py` | 3432 | 成立，有前提 | `ParsePaperUseCase` 1372 行，2 个公开方法；`parse` 一个就 604 行、用到全部 16 个私有属性，按方法分组切不开，要先把 `parse` 本身按阶段拆开。分组：来源解析、身份合并、文档解析与重试、分块清单、事件与重放；解析缓存身份（L322-686，约 365 行）和质量报告（`_quality_report`，294 行）各成模块。两个替身见 5.5 |
| `backend/research/rag/evaluation/paper_benchmark_suite.py` | 3793 | 搬进评测包时拆 | 没有主类，4 个公开函数加 14 个模型或 judge 类。按前缀分：模型、问题审计、候选评估与证据包补全、回答样本与 judge、人工抽检、gold judge 与修复、策略晋级清单（192 行）、报告与 Markdown（`_suite_markdown` 345 行）、指标与抽样工具 |
| `interfaces/services/mcp_service.py` | 3393 | 成立，补两点 | `MCPApplicationService` 1279 行、75 个方法，超过单类上限；`call_tool`（119 行）是一长串 `if tool_name ==`，改成注册表。分组：工具/资源/提示词定义（`_tools` 一个就 633 行）、能力与权限、服务工厂、资源 URI 参数解析、工具参数校验、按领域分的处理方法（research/report/source/entity/subscription/memory/run/graph_wait/event_operator） |
| `interfaces/composition/research.py` | 3208 | 成立 | `_build_configured_composition` 790 行（L1890 起），按顺序装配 17 个子系统，每个成为一个装配函数：执行组合与来源运行时；来源策略与连接器；目录存储与文档编译器；LLM 设置与模型客户端；GitHub、文档与目录服务、来源解析器；解析用例；制品与图结果存储；Harness 等待；持久事件与图索引；动态任务计划（约 200 行内嵌工厂）；生产解析评测服务；阅读器修复；上下文组装与 RAG；恢复源与对账；活动存储校验与运行时；问答 worker；RAG 问答与应用服务。`_DurableResearchRunRecoverySource`（360 行）单独成模块 |
| `interfaces/services/research_desktop_service.py` | 2995 | 成立，但只拆函数不够 | 模块级投影函数（L1580 起）按阅读器投影、操作结果、报告与引用、分析与质量投影、个人记忆、通用值处理分组。`ResearchDesktopApplicationService` 本身 1357 行，拆协作对象：准备与导入、分析作业生命周期、问答（`submit_ask` 152 行）、操作对账（`_reconcile_operation` 217 行）、报告保存与导出 |
| `interfaces/services/research_service.py`（初稿漏列） | 2598 | — | `ResearchApplicationService` 1345 行。拆成：`models.py`（Input/Error 类型）、`ports.py`、`actor_scope.py`、`projections.py`（阅读器答案与引用）、`errors.py`、游标与参数工具；服务类按协作对象拆为目录与来源、分析与运行投影、阅读器与问答、追踪与诊断制品、运行记录守卫。`InMemoryResearchRunStore` 见 5.5 |
| `interfaces/api/app.py` | 1253 | 部分不成立 | 初稿说的“剩余内联路由”已经没有了，只剩 L577-581 手动 extend 的 `personal_memory` 路由，改走 `include_routers`。`create_app`（451 行）拆出：`auth.py`（API Key 与 RBAC，L921-1105）、`audit.py`（L1123-1212）、`sse.py`（L807-862）、`payloads.py`（L672-800）、`middleware.py`（`create_app` 内的几个中间件） |

前端：

超过 1000 行的 7 个文件（行数与初稿一致），另有 27 个在 501–1000 行之间：

- `lib/i18n/translations.ts`（2195 行）— 成立但要更细：`en`（L1-1094）和 `zh`（L1098-2191）两份，类型由 `keyof typeof en` 推导。按一级命名空间拆后 `studio`（525 个键，中英合计约 1050 行）仍超过上限，要再按二级拆（`runs`、`ops`、`artifacts`、`evidence`、`review`、`quality`、`sources` 等）。每个命名空间文件同时导出 en 和 zh，入口展开合并后推导类型。
- `components/papers/open-reader/open-reader-page.tsx`（1477 行）— 成立。主组件 418 行（7 个 useState、9 个 useEffect）：分段渲染窗口、章节滚动定位、选区回答回填、材料同步、三个弹层的状态各抽成 hook。子组件已经写成函数，只需搬到单独文件：`PaperTable` 与表格样式、段落分片纯函数（L922-1047）、`ReaderAssistDrawer`/`AssistAnswerCard`、`FloatingToc` 等四个浮层组件、文案表 `readerCopy`（122 行）。
- `components/papers/shared/paper-pdf-viewer.tsx`（1371 行）— 成立。主组件 593 行（14 个 useState、7 个 useEffect）：文档加载、全文抽取与搜索缓存、页面与文本层渲染、初始页定位、搜索高亮抽成 hook；工具栏和选区笔记弹层还写在主组件里，要拆出；缩略图渲染队列和搜索纯函数单独成模块。
- `features/portal/reader/reader-assistant.tsx`（874 行）— 成立。10 个只保存最新值的 ref 合成一个 `useLatestRef` 工具；请求状态、`applyOperation`、副作用和 `submit`（160 行）合成一个提问操作 hook；界面拆成答案区、提问表单、笔记面板三个子组件。
- `lib/papers/real-data.ts`（1336 行）— 部分成立。文件按数据来源和处理阶段组织：接口加载与数据集组装、任务与方法分类推导（和只被测试用的 `taxonomy-fallback.ts` 重复）、本地缓存与 artifact 文件读取（用 fs，只能服务端）、信号字段抽取、通用工具。只有分类推导那段按实体拆。
- `lib/projects/mapper.ts`（1331 行）— **不成立**。文件里只有 Project 一个实体，按处理阶段拆：参数规范化与列表组装、过滤/排序/统计、候选抽取与映射（含仓库识别、关联引用）、评分、合并去重、标签与枚举规范化。
- `features/evidence-graph/evidence-graph-data.ts`（1172 行）— 基本成立。五个看板实体的加载与候选映射按实体拆；其余按图构建、评分与轨迹、查询解析、文本工具分。
- `features/projects/components/projects-product-page.tsx`（1183 行）— 成立。六个路由视图各成文件；Lab 系列组件已被另外三个文件导入，先拆成独立模块。

完整的超限清单由 6 节的检查脚本生成，上表以外的文件按同样方法处理。

### 5.4 合并过碎的文件

不是所有小文件都要合并。端口、错误类型、配置模型这类“一个概念一个文件”的小文件是有意的，保留。要处理的是这两种：

- **空壳实现**：类只有一行转发，例如 `framework/memory/retrieval/hybrid_strategy.py` 的 `HybridMemoryRetrievalStrategy.search` 直接返回 `store.search(query)`。没人用就删，有人用就让调用方直接用 store。
- **同一职责被切成多个几行的文件**，而且总是一起被导入。按职责合并：

候选（复核结论：按模块路径和符号名全仓库搜索，含字符串引用和 `__init__.py` 懒加载表）：

| 现在 | 结论 | 证据 |
| --- | --- | --- |
| `framework/memory/retrieval/{hybrid,keyword,temporal}_strategy.py`、`reranker.py` | 全部删除，初稿的合并目标 `strategies.py` 不需要 | 只出现在 `retrieval/__init__.py` 和 `memory/__init__.py` 的导出；backend 用自己的 `IntelligenceMemoryReranker`（`backend/memory/intelligence_recall.py:16`） |
| `framework/memory/stores/{graph,keyword,temporal,hybrid}.py` | 全部删除，`protocols.py` 不需要 | 只出现在 `stores/__init__.py:2-15` 和 `memory/__init__.py` 的导出（命中的 `PostgresGraphMemoryStore` 是另一个类） |
| `framework/memory/policy/{consolidation,forgetting}_policy.py` | 全部删除，`policy/models.py` 不需要 | 只出现在 `policy/__init__.py` 和 `memory/__init__.py` 的导出 |
| `framework/memory/context/{budget,citation,compression}.py` | 合并，连同 `formatter.py`（64 行）合成 `framework/memory/context/formatting.py`（约 95 行）；初稿目标名 `models.py` 不合适，citation 和 compression 是行为 | 生产在用：`memory/runtime/context_assembler.py:3-31` 用它们作默认值，`runtime.py:29,61`、`recall.py:7` 用 `MemoryContextAssembler` |
| `framework/llm/prompts/`（`renderer`、`variables`、`template`） | 整个包删除，不合并 | `PromptRenderer`、`PromptTemplate`、`PromptVariables` 只出现在 `llm/__init__.py:110,285-287` 和 `tests/framework/llm/test_clients_cache_prompt_redaction.py` |
| `backend/foundation/feedback/{feedback_collector,feedback_aggregator}.py` | `feedback_collector.py`（11 行）删除；`feedback_aggregator.py` 合并的前提不成立，先复核整个 feedback 包 | `FeedbackCollector` 只出现在 `feedback/__init__.py:2,36`。`FeedbackAggregator` 被 `runtime_closure.py:17,75`、`feedback_learning.py:7,30` 使用，但这两个模块的入口 `FeedbackLearningService`、`build_runtime_quality_closure` 在 `backend/foundation` 以外只有测试使用（`foundation/__init__.py:79-80` 只是重新导出）。没人用就整包删除；有人用就把 `group_by_type` 并进 `feedback_learning.py` |
| `interfaces/services/model_connections/`（15 个文件，模型连接新增） | 保留，不合并 | 逐个判断后没有空壳，也没有被切碎的职责：每个文件是一个独立概念（错误码、目录解析、加密、存储形状、持久化适配器、对外视图、一次性迁移……）；最小的 `messages.py`（39 行）有 3 个包外调用方单独导入。`admission.model_serves` 和 `ConnectionSpec.admission_error` 重复了同一段判断，去重即可，不需要合并文件 |

合并后原路径不保留转发模块，调用方一次改完。完整候选清单由 6 节的检查脚本列出（≤ 15 行的非 `__init__` 模块），逐个按上面两条判断。

### 5.5 生产目录里的测试替身和假数据

**后端替身。** 按文件名统计是 17 个 `fake*.py`/`testing.py`/`fixtures.py`（2780 行），但还有一批替身类写在名字正常的文件里，按文件名统计不到，按类名补进清单。复核后逐个处理：

| 替身 | 生产里的引用 | 处理 |
| --- | --- | --- |
| `framework/harness/side_effects/fake.py`（941 行） | `backend/research/application/single_paper_runtime.py:49,1208` 写成默认值 `side_effect_store or InMemoryHarnessSideEffectStore()`；正式装配 `composition/research.py:2068-2070` 一直显式传入 `SQLiteHarnessSideEffectStore` | 挪到 `tests/support/`，默认值改成必须显式传入 |
| `framework/harness/context/fake.py`、`framework/harness/rag/fake.py` | 只有导出表和另一个替身引用；`rag/__init__.py:7` 在导入时加载 `rag.fake` | 一起挪到 `tests/support/`，删掉 `rag/__init__.py:7` |
| `framework/harness/workers/fake.py` | 只有导出和 `subagents/fake.py:30`、`skills/__init__.py:56` | 挪到 `tests/support/`；同步改 `tests/architecture/test_harness_durable_event_boundary.py:32` 登记的路径 |
| `framework/llm/clients/fake.py` 的 `FakeLLMClient` | 冒烟命令 `interfaces/services/agent_loop_smoke_service.py:79,869` 在用 | 初稿说的“给冒烟命令一个明确的离线实现并起正式名字”对应的是这个文件（不是 harness 下的三个）：改名为离线脚本化客户端，留在生产目录 |
| `FakeSubAgentGateSuite`（`harness/subagents/gates.py:310`） | `subagents/runtime.py:72` 的默认值，`agent_loop/child_executor.py:113-114` 还检查它的具体类型 | 起正式名字，或改成必须显式传入（**需要你拍板：它是否就是生产要用的门禁**） |
| `FakeRAGContextPackAssembler`、`FakeSourceVerifier`（`harness/rag/__init__.py:5,58`） | 只有导出 | 挪到 `tests/support/` |
| `infrastructure/storage/vector/fake_store.py` 的 `InMemoryVectorStore`、`vector/embeddings.py:72` 的 `FakeEmbeddingAdapter` | 只有导出；生产只用父类 `DeterministicEmbeddingModel` | 挪到 `tests/support/`，父类保留 |
| `interfaces/events/audit.py:19` 的 `InMemoryAuditSink` | 只有 `interfaces/events/__init__.py:3` 导出 | 挪到 `tests/support/` |
| `interfaces/services/research_service.py:198` 的 `InMemoryResearchRunStore` | `research_service.py:307` 写成 `run_store or _DEFAULT_RESEARCH_RUN_STORE`，在 L2573 留了一个进程级内存默认实例；两条正式装配都显式传入（`composition/research.py:1791,2647`） | 挪到 `tests/support/`，去掉默认值 |
| `interfaces/api/rate_limit.py:22` 的 `InMemoryRateLimiter` | `api/app.py:1114`、`api/hosted_middleware.py:44-54` | **保留**：它就是生产实现，名字里有 InMemory 但不是替身 |
| `backend/research/application/single_paper_runtime.py:1366-1367` 的 `_CompatibilityResearchArtifactBundleHandler`（L2926，162 行） | `artifact_handler_factory is None` 时的默认值；文档写明 “Test-only”，reason_code 是 `prepared_test_fake`、`published_test_fake` | 挪到 `tests/support/`，默认值改成必须显式传入 |
| `single_paper_runtime.py:2868` 写死的 `package_hash: "sha256:fake-research-skill"` | 在生产路径上 | 改成由装配传入真实值 |
| `backend/research/application/parse_paper.py` 的 `MetadataOnlySourceResolver`（L177）、`InMemoryResearchEventSink`（L240） | 前者是 `ParsePaperUseCase` 的默认值（L709），后者被 `application/__init__.py:20,93` 导出 | 挪到 `tests/support/`，默认值改成必须显式传入 |
| `InMemoryResearchCatalogRepository` | 见 5.1 | 挪到 `tests/support/` |

**前端假数据。** 按项目规则“项目代码用真实数据”处理，复核后情况比初稿复杂，分四类：

1. **开发模式开关**：`lib/api/client.ts:148-152` 的 `shouldUseMock` 读 `NEXT_PUBLIC_NEWSROOM_USE_MOCKS`，部署配置、`frontend/` 和 `.github/` 都没有设置这个变量。删除开关和 `lib/api/mock-data.ts`（90 行）。
2. **接口没数据时回退到假数据**：`features/sources/hooks/use-sources.ts:27`（`data?.length ? … : mockSources`）。初稿漏了两处，都在生产页面上：`features/studio/runs/lib/mock-agent-runs.ts`（445 行；`/studio`、`/studio/runs` 在接口无记录或出错时显示假运行，单条运行缺字段时也用假数据补齐，见 `run-center-adapter.ts:41-43,81,85-94`）、`features/studio/boards/lib/board-fallback-data.ts`（273 行；看板接口不可用时显示假卡片，见 `board-adapter.ts:53,92,162-163`）。这三处改成显示空状态或错误状态。
3. **只有假数据，根本不调接口**（初稿误归为“回退”）：`features/tech/hooks/use-tech-items.ts:15`、`features/topics/hooks/{use-topic-list,use-topic-detail}.ts`、`features/topics/components/topic-timeline.tsx:43`、`lib/search.ts:30-67`，对应 `/tech`、`/topics`、`/topics/[id]`、`/search` 四个页面。这几个页面没有任何真实数据来源，**已决定删除**，连同只服务它们的 hook、组件、假数据和导航入口。
4. **只被测试用到**（初稿误归为“回退”）：`features/memory/hooks/use-memory-search.ts`、`features/quality/hooks/use-quality-results.ts`、`features/studio/artifacts/hooks/use-artifacts.ts`，按 5.1 删除。

`lib/mock-data.ts`（966 行）在上面几类处理完后只剩测试使用，挪到 `src/test/fixtures/`，和目录里已有的文件合并（5 个现有文件没有测试使用，删除）。`lib/papers/taxonomy-fallback.ts`（112 行）只被测试使用，`real-data.ts:348-483` 已有同样功能的推导，删除。`features/admin/lib/mock-data.ts` 见 5.1。

### 5.6 精简包的导出表

`__init__.py` 合计 16464 行。`framework/harness/__init__.py` 有 1943 行、约 617 个懒加载导出项，`framework/events/__init__.py` 有 1093 行。包内部代码已经直接从定义模块导入，包外只有 22 个生产文件从 `framework.harness` 根导入（例如 `interfaces/composition/research.py`、`backend/research/application/single_paper_runtime.py`、`infrastructure/research/context_runtime.py`），18 个生产文件从 `framework.events` 根导入（例如 `interfaces/api/app.py`、`infrastructure/storage/workers/redis_queue.py`）；测试分别约 123 个和 44 个文件。

规则：

- 只有层的公开入口保留导出表（例如 `framework.harness`、`framework.events`、`framework.llm`），只列包外确实在用的名字。
- 其他子包的 `__init__.py` 不写导出表，只写一行模块说明。
- 包外的导入改成从公开入口或定义模块导入，二选一，同一个名字全仓库只有一种写法。
- `__init__.py` 上限 150 行，纳入 6 节的检查。
- 现有 `tests/architecture/test_framework_export_surfaces.py`、`test_framework_public_api.py` 按新的导出表更新。

### 5.7 依赖路径的架构测试

`tests/architecture/` 有 77 个架构测试，其中不少按文件路径登记（`fixtures/graph_only_freeze_baseline.json` 112 个路径，`fixtures/harness_graph_caller_inventory.json` 144 个路径）。拆分和移动文件后，这些清单要用各自的生成脚本重新生成，不能手改；生成后检查差异只有路径变化，没有新增调用方。

复核时确认要同步更新的登记：删 `framework/tool/inspection/testing.py` 时改 `test_harness_execution_and_child_caller_boundaries.py:13-28` 的白名单；挪 `harness/workers/fake.py` 时改 `test_harness_durable_event_boundary.py:32`；删 `InactiveGraphStorageIndexAdapter` 时改 `test_graph_storage_index_boundary.py:20-60`。

另外，`graph_only_freeze` 测试会拒绝新出现的 `LEGACY_*` 标识符（这个前缀在仓库里专指已退役的 workflow 运行时）。拆分和改名时不要用 `legacy` 命名别的东西；模型连接的迁移就因此把旧单 Key 存储命名为 `single_key`。

### 5.8 跨端一致性（复核新增）

模型连接引入了两处前后端各写一份的逻辑，其中一处已经出现实际分歧，属于缺陷，不是整理项，建议在整理开始前单独修复：

**窗口准入的思考预留（缺陷）。** 用户没选思考强度时：

- 后端 `resolution.py:93-96` 让 effort 保持 `None`，`reasoning.py:97` 把 `None` 按 medium 预算，预留 8192；
- 前端 `model-connection-draft.ts:164` 用模型自己的默认档查预留；
- 真实目录里 `deepseek-flash`、`deepseek-v4-pro`、`claude-sonnet-5-5` 的默认档是 `high`（`configs/hosted-llm-providers.yaml:97,109,157`），前端预留 16384，后端预留 8192。

两边结果不同，而且**错的是后端**：不发 effort 时 DeepSeek 实际按 high 思考，后端少预留了一半。修法：后端在 effort 为 `None` 时按目录声明的默认档预算（只影响预算，不改变发送内容）；前端随之一致。`draftFit` 目前没有单元测试。

**防止以后再分歧（已决定：方案 A）：**

- A. 后端提供预览接口 `POST /account/model-connections/preview`：`ModelConnectionService.preview_fits(draft)` 复用 `validate_draft` → `resolve_connection_spec` → `views._fit`，前端输入时防抖调用，删掉 `draftFit` 和只为它下发的 `reasoningOutputAllowance`。只有一份算法；代价是一个新接口，输入时多一次请求。
- B. 保留前端即时计算，加契约测试：后端对目录里每个模型 × 每个思考档（含未选）× 每个用途算出结果，写成 `frontend/src/test/fixtures/model-connection-fits.json`，前端测试读它跑 `draftFit` 逐条比对。不加接口；代价是两份算法继续并存。

**错误码清单。** 后端 `errors.py` 有 17 个码，另有 `model_probe_rate_limited` 在 `api/hosted_middleware.py:99`。前端在 5 处各写一份：`lib/account/client.ts`（文案）、`lib/account/settings-navigation.ts`（7 个“去设置页修”）、`components/account/model-connection-action.tsx`（3 个“重新填 Key”）、`settings/model-connections-section.tsx:256-258`（`unfitReason`）、同文件 `:103`（冲突判断）。新增一个码最多要改 5 处，漏改不会报错，只会落到兜底文案。另有两处具体问题：

- 研究链路的中文提示在后端 `messages.py` 和前端 `client.ts` 各有一份，同一个码写法不同（`key_rotated` 一处写“服务的加密密钥”，一处写“服务器的加密密钥”）。`errors.py` 的文档说浏览器按码显示自己的中文，和 `messages.py` 的存在矛盾，只留一份。
- `research-capability-notice.tsx:148-153` 用子串 `"connection"` 判断“不可达”，所有 `model_connection_*` 都会命中；现在靠前面先筛掉才没出错，规则要改成按码判断。

修法：前端建一个模块（例如 `lib/account/model-connection-errors.ts`），一张 `code → { message, remedy, fitLabel }` 的表，上面 5 处都从它推导；后端 `errors.py` 加一张码表（含限流码），加一个契约测试比较两边的码集合（仓库里已有读前端源码的先例：`tests/interfaces/test_product_brand_identity.py:38`）。用途枚举 `model-connections.ts:8` 改成遇到未知用途时宽容解析，避免后端先上线新用途时整个设置页解析失败。

### 5.9 仓库配置（复核新增）

- **`.gitignore` 第 17 行的 `lib/`**（Python 打包留下的规则）会忽略所有名为 `lib` 的目录，前端每个 `lib` 目录都靠 `!` 规则单独放行。`features/admin/lib/` 没有放行，所以 `features/admin/lib/{mock-data,i18n}.ts`（789 行）从未进入 git，干净 clone 下 `admin-console` 的导入解析不到。以后任何新的 `features/*/lib/` 都会被同样静默忽略。改成只在仓库根目录生效（`/lib/`），删掉前端那串放行规则。
- **`frontend/tsconfig.json` 排除了 `src/features/**/*`**，另外排除了 `src/lib/mock-data.ts`、`src/lib/search.ts` 和四个 page-client。被排除的文件只有被页面导入时才会类型检查，死代码和解析不到的导入因此长期没人发现。整理完成后去掉这些排除项，`tsc --noEmit` 覆盖整个 `src/`。
- **`frontend/root-env.mjs` 和 `root-env.ts`** 逻辑逐行相同，只差类型标注；`.mjs` 给 `next.config.mjs`，`.ts` 给 `vitest.config.ts` 和两个 playwright 配置（`playwright.paper-agent.config.ts` 不加载）。按 5.1 末尾的方案只留 `.mjs`，类型声明照 `deployment-config.mjs` + `deployment-config.d.mts` 的现成写法补一个 `.d.mts`。两边取目录的方式也要统一（一处用 `process.cwd()`，一处用 `import.meta.url`）。

## 6. 防止回退的检查

新增 `tests/architecture/test_codebase_hygiene.py` 和前端对应的检查脚本，进 CI：

| 检查 | 内容 |
| --- | --- |
| 文件大小 | 源文件、`__init__.py`、测试文件按 5.3、5.6 的上限检查。整理期间允许一份 `tests/architecture/fixtures/oversized_files.json` 清单，只能删条目不能加，阶段 4 结束时清空 |
| 评测依赖方向 | `framework`、`backend`、`infrastructure`、`interfaces` 不得导入 `evaluation` |
| 测试替身位置 | 业务包里不得出现 `fake*.py`、`testing.py`、`fixtures.py`；前端 `src/` 里除 `src/test/` 外不得导入 `fixtures` |
| 死代码 | Python 用 vulture 扫描四个业务包和 `evaluation`（置信度 80 以上报错，误报写进白名单并注明原因）；前端用 knip 扫描没有引用的文件和导出 |
| 重复文件 | `frontend/root-env` 这类同名不同扩展的拷贝只允许一份 |
| 类型检查覆盖 | `frontend/tsconfig.json` 不得排除 `src/` 下的源文件（5.9） |
| 被忽略的源文件 | 有源文件导入了 `git ls-files` 之外的路径即失败，防止 `.gitignore` 再静默吞掉代码（5.9） |
| 跨端契约 | 5.8 的错误码集合和窗口准入结果，前后端必须一致 |
| 生产默认值里的替身 | `x or InMemory*()`、`x or Fake*()` 这种用替身做默认值的写法在业务包里报错（5.5） |

vulture 和 knip 加进开发依赖，CI 的检查命令统一走 `python -m scripts.dev hygiene` 和 `npm --prefix frontend run hygiene`。

## 7. 实施步骤

每一步单独提交、单独跑全部测试，任何一步都能单独回退。步骤之间按顺序进行，前一步合进 main 再开始下一步，避免长期分支冲突。

| 步骤 | 内容 | 验证 |
| --- | --- | --- |
| 前置 | 修 5.8 的思考预留缺陷（后端按目录默认档预算，前端一致，补 `draftFit` 单元测试）；按 5.8 的决定加预览接口或契约测试。这是缺陷修复，不等整理 | 模型连接相关测试全部通过；deepseek-flash 未选思考强度时前后端预留一致 |
| 0 | 建立基线：跑全部测试，记录结果；把统计脚本放进 `scripts/`；加上 6 节的检查和超限清单（先只记录，不失败）；修 5.9 的 `.gitignore`，确认 `features/admin/lib/` 的去留 | 全部测试通过，统计数字与第 4 节一致 |
| 1 | 删除死代码（5.1），先 Python 后前端，每个目录一个提交 | 全部测试通过；`news` 全部子命令 `--help` 正常；托管栈冒烟（登录、上传论文、解析、分析、提问） |
| 2 | 测试替身和假数据归位（5.5） | 同上；前端开发模式各页面在接口无数据时显示空状态 |
| 3 | 评测代码独立成包（5.2） | 评测相关测试全部通过；CI 的评测门禁结果与基线一致；生产镜像内没有 `evaluation` 目录 |
| 4 | 拆分超大文件（5.3），按 5.3 表格顺序一次一个文件 | 每个文件拆完跑全部测试；超限清单删掉对应条目 |
| 5 | 合并过碎文件、精简导出表（5.4、5.6） | 全部测试通过；架构测试清单重新生成 |
| 6 | 打开 6 节检查的失败开关，更新 `docs/architecture/` 和 `AGENTS.md` 里的目录说明 | CI 全绿 |

## 8. 验收标准

1. 对外行为不变：HTTP API 的 OpenAPI 文档（`docs/api/openapi.json`）与基线一致；`news` 命令和参数一致；MCP 工具列表一致；事件 schema 目录一致。
2. Python、前端、桌面端测试全部通过，测试数量的减少只来自随死代码一起删除的测试，并在提交说明里列出。
3. 6 节的检查全部开启且通过，超限清单为空。
4. 托管栈端到端验证通过：用 `deploy/hosted` 编排起服务，完成登录、上传论文、解析、分析、提问、创建用户；本机模式的真实 Docker e2e 通过。
5. 生产 api 镜像里不包含评测代码和测试替身。
6. 第 4 节的统计重新跑一遍，结果写进本文档附录 B，作为整理后的基线。

## 9. 风险与应对

| 风险 | 应对 |
| --- | --- |
| 静态分析漏掉动态加载（懒加载导出、`importlib`、技能包注册、配置里的类名），误删仍在用的代码 | 每个删除候选全仓库搜索名字；删除和其他改动分开提交；全部测试 + 托管栈冒烟后再合并 |
| 拆分文件时顺手改了逻辑，评审看不出来 | 拆分提交只允许搬动代码和改导入；用 `git diff --color-moved` 检查 |
| 按路径登记的架构测试清单大面积变化 | 只用生成脚本重新生成，差异里不允许出现新的调用方 |
| 和并行开发的分支冲突 | 模型连接已全部合进 main（752b4149 至 696d593b），初稿这条风险不再存在。步骤 4 一次只拆一个文件、拆完立即合并；动 `framework/harness/**` 前先确认没有其他分支在改同一文件 |
| 两处拆分不是原样搬动：`router.py` 的 `_complete`/`_stream` 去重、事件存储两份实现统一异常类型 | 各自单独提交、单独评审，不和纯搬动的提交混在一起；去重前先补齐两条路径的测试 |
| 前后端的列表和算法以后再分歧（见 5.8） | 用 5.8 的契约测试或预览接口兜住，进 CI |
| 前端去掉假数据回退后，开发模式下部分页面变空 | 这是预期结果，页面显示明确的空状态；需要演示数据时用真实接口导入真实数据 |

## 10. 需要拍板的决定（复核新增）

| # | 问题 | 选项 | 建议 |
| --- | --- | --- | --- |
| 1 | 思考预留缺陷怎么防止再分歧（5.8） | A. 后端预览接口，前端删掉 `draftFit`；B. 保留前端计算，加契约测试 | **已决定：A** |
| 2 | `/tech`、`/topics`、`/topics/[id]`、`/search` 四个页面只有假数据（5.5） | 接真实接口；或删掉页面 | **已决定：删除** |
| 3 | `InactiveGraphStorageIndexAdapter` 一组（5.1） | Gate B 还在计划里就保留并写明启用门禁；不在就删除 | 取决于 Gate B 是否还做 |
| 4 | `PostgresConversationStore`、`redis_runtime.py`（5.1） | 补装配入口；或删除（迁移文件保留） | 删除：生产只用 JSON 存储 |
| 5 | `FakeSubAgentGateSuite` 是生产默认门禁（5.5） | 起正式名字作为生产实现；或改成必须显式传入 | 先看它是否就是生产要用的门禁，再定 |
| 6 | `paper-pdf-viewer.layout.browser.mjs`（5.1） | 加进 CI；或删除 | 加进 CI：它测的是 PDF 布局，其他测试覆盖不到 |
| 7 | 步骤 4 拆超大文件的范围 | 全部 59 个超限 Python 文件；或先做改动最频繁的（`task_plan/{parallel,replay,stage}.py`、`events/schema/catalog.py`、`composition/research.py`） | **已决定：全部 59 个 Python 和 7 个 TS 超限文件，仍一次一个文件提交** |
| 8 | 旧板块流水线（signal → extraction → relation → analysis → output，5.1） | 整条复核后删除只剩测试在用的部分；或只删 relation 和 analysis/quality | 整条复核：relation 和 quality 已确认没有生产导入方，其余几层看起来也一样 |
| 9 | `scripts/graph_only_migration/` 的迁移本身（5.1） | 还有环境保存迁移前的数据就保留；没有就删除 | 需要你确认部署情况，代码里查不出来 |
| 10 | `stale_reference_audit.py`、`ProtocolBudgetLedger`、`run_business_answer_eval.py`（5.1） | 删除；或作为手动工具保留 | 删除：都没有任何入口调用 |
| 11 | 根目录 4 份 `codex-handoff-*.md`（5.1） | 移到 `docs/plans/archive/`（等于移出 git，因为 `docs/` 被忽略）；或留在仓库 | 先确认是否还要保留在仓库里 |

## 附录 A：统计方法

- 文件行数：`git ls-files` 列出的源文件逐个计行，测试按路径（`tests/`、`__tests__/`、`*.test.*`、`*.spec.*`、`e2e/`）区分。
- Python 依赖图：用 `ast` 解析全部模块的 `import`、`from ... import` 和形如 `"包.模块"` 的字符串引用，从 `interfaces` 的 CLI、API、MCP、worker 入口出发求可达模块；从测试、`scripts/`、`data/`、`examples/`、`sdk/` 出发分别求可达集合，用来区分“没有引用”“只有脚本用”“只有测试用”。
- Python 顶层符号：一个模块里的顶层类或函数，在本模块内没有被引用，并且在其他业务模块、配置文件里都不出现这个名字，计为没有引用。
- 前端依赖图：解析 `import`、`export ... from`、动态 `import()` 和 `vi.mock`，从 Next 的 `page`、`layout`、`route` 等约定文件和 `proxy.ts`、配置文件出发求可达文件。

## 附录 B：整理后的统计

（步骤 6 完成后填写）
