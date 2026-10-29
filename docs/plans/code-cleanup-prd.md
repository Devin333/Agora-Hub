# 代码整理 PRD

状态：待评审 · 日期：2026-10-29 · 基线：`main` @ ca299c6f

## 1. 背景

项目从 Agora Hub（新闻、项目、报告、工作室后台）演进到托管版论文工作台，期间经历了 graph-only 编排迁移、本地桌面运行时移除、瘦客户端改造等多轮重构。功能都还在，但代码里积累了几类问题：

- 一部分代码已经没有任何地方调用，或者只剩测试在调用。
- 评测代码（RAG 评测协议、解析对比、基准套件）和业务代码混在 `backend/research` 里，生产镜像也一起打包。
- 文件大小两极分化：有 36 个源文件超过 2000 行，最大的 7276 行；同时有 59 个 Python 模块不到 15 行，很多只是一个空壳类。
- 测试替身（`fake.py`）和前端假数据（`mock-data.ts`）放在生产代码目录里，前端有几个页面在接口没返回数据时直接显示假数据。

这些问题让新改动很难定位、评审很难看全、出错时影响面难判断。本次整理的目标是降低后期维护成本，不改变任何对外行为。

## 2. 已确认的决定

| 问题 | 决定 |
| --- | --- |
| 托管版用不到的旧功能（新闻与来源采集、项目、报告、工作室、门户首页、订阅、MCP、SDK、apps/web） | 全部保留，只删没有任何地方使用的死代码 |
| 评测代码 | 保留，挪到独立的评测包；生产代码不再依赖它，生产镜像不再打包 |

## 3. 目标与非目标

目标：

1. 删除没有调用方的代码，以及只被自身测试调用、也不属于评测的代码。
2. 评测代码独立成包，依赖方向只能是评测依赖业务，不能反过来。
3. 把超大文件按职责拆开，把过碎的文件按职责合并，定下文件大小的规则。
4. 生产代码目录里不再有测试替身和假数据。
5. 把规则写成架构测试，放进 CI，防止整理完又长回去。

非目标：

- 不改 HTTP 接口、CLI 命令、MCP 工具、事件格式、数据库结构和存储文件格式。
- 不删功能，不改功能行为，不做性能优化。唯一的行为变化是 5.5：前端开发模式不再用假数据填充页面。
- 不顺手重写算法或改命名风格；拆分文件时代码原样搬动，只调整导入。
- 不改桌面端（刚完成三种连接方式改造，体量正常）。

## 4. 现状数据

统计范围：git 跟踪的文件；“源文件”不含测试。统计脚本见附录 A，实施前会重新跑一次。

| 指标 | 数值 |
| --- | --- |
| 源文件（py/ts/tsx/js） | 2434 个 |
| 源文件 ≥ 1000 行 / ≥ 2000 行 / ≥ 4000 行 | 113 / 36 / 8 个 |
| Python 非 `__init__` 模块 ≤ 15 行 | 59 个（`framework/memory` 66 个模块里占 16 个） |
| 测试文件 ≥ 1000 行 / ≥ 2000 行 | 40 / 8 个 |
| `__init__.py` 总行数 | 16373 行，其中 31 个是懒加载导出表；最大的 `framework/harness/__init__.py` 1937 行 |
| 没有任何引用的 Python 模块 | 13 个，456 行 |
| 没有任何引用的 Python 顶层类和函数 | 244 个，3118 行 |
| 只被测试引用的 Python 模块 / 顶层符号 | 78 个模块 21942 行 / 297 个符号 14838 行（其中一大半是评测代码） |
| 生产目录里的测试替身 | 17 个文件，2793 行 |
| 前端没有任何页面用到的文件 | 58 个，3746 行 |
| 前端只被测试用到的文件 | 14 个，1302 行 |
| 评测相关代码 | `backend/research/rag/evaluation` 52 个文件 35027 行，另有 CLI、端口和适配器约 20 个文件 |
## 5. 整理内容

### 5.1 删除死代码

判定标准：一个模块或顶层符号，从生产入口（`news` CLI、HTTP API、MCP、worker、Next 页面和路由、桌面端）、脚本和评测入口都走不到，并且 `configs/`、`backend/foundation/skills/**`、`sources.yaml` 这类配置文件里也没有按名字引用它。只被它自己的测试引用的代码同样算死代码，和测试一起删除。

静态分析会漏掉按字符串动态加载的代码（懒加载导出表、`importlib`、技能包注册），所以每个候选都要人工确认：全仓库搜索名字，再跑完整测试。

Python 候选（已确认没有引用的模块，删除前逐个复核）：

| 模块 | 行数 | 说明 |
| --- | --- | --- |
| `backend/research/application/batch_ingest.py` | 106 | 全仓库没有引用 |
| `backend/research/rag/evaluation/paper_system_eval.py` | 105 | 被 `data/eval/run_system_eval.py` 取代 |
| `backend/layers/output/output_quality_checks.py` | 52 | 全仓库没有引用 |
| `infrastructure/external/source_adapters.py` | 52 | 全仓库没有引用 |
| `backend/layers/signal/quality_checks.py`、`backend/layers/extraction/quality_checks.py` | 62 | 全仓库没有引用 |
| `backend/layers/signal/deduplicator.py` | 17 | 只在技能说明文档里出现名字，需确认 |
| `backend/research/rag/cli/run_parser_bakeoff_ingest.py`、`run_parser_url_bakeoff_ingest.py` | 15 | 只转发到评测模块的 `__main__`，挪包时一起处理 |
| `backend/research/memory/`（含 `reader_repair_memory.py`） | 25 | 名字和 `graphs/reader_repair*` 相近，需确认是否真没用 |
| `backend/layers/analysis/ranking_features.py` | 7 | 同名字段在别处使用，需确认 |

没有引用的大块符号（先看这几个，其余 236 个按同样方法处理）：`backend/layers/analysis/pipeline.py` 的 `AnalysisPipeline`（298 行）、`framework/tool/inspection/testing.py` 的 `ToolTestRunner`（135 行）、`framework/harness/control_plane/replay_history.py` 的 `harness_decision_input_snapshot`（114 行）、`backend/research/rag/evaluation/paper_gold_builder.py` 的 `GoldenSetBuilder`（69 行）、`framework/shared/lazy_exports.py` 的 `LazyExports`（67 行）。

只被测试使用的非评测代码，逐个判断：

- 生产里已有正式实现、这个只是给测试用的内存版或简化版：挪到 `tests/support/`。
- 给可选部署留的适配器（Redis、Qdrant、Postgres 相关）：保留，但必须有一个装配入口按配置走到它，并在模块文档里写明由哪个配置启用。没有装配入口的按下一条处理。
- 既不是上面两种、测试只是在测它自己：连同测试一起删除。

整块只剩测试在用的目录：`backend/layers/relation/`（关系抽取与证据校验）和 `backend/layers/analysis/quality/`（引用检查、支撑矩阵、编辑门禁），合计 4494 行。生产代码里唯一的引用方是已经没有调用者的 `AnalysisPipeline`，`backend/layers/analysis/tools.py` 用的是另一个模块 `quality_records`。确认后整块连同测试删除，并从 `scripts/dev.py` 的测试清单里去掉。

需要逐个归类的较大符号：`ProtocolBudgetLedger`（806 行，评测）、`PostgresConversationStore`（488 行）、`FakeSkillEvolutionPort`（334 行）、`RagasJudgeAdapter`（326 行，评测）、`ScoringRuntime`（304 行）、`InactiveGraphStorageIndexAdapter`（302 行）、`TaskPlanRecoveryService`（245 行）、`ToolBatchExecutor`（236 行）、`InMemoryHarnessSignalInbox`（230 行）、`ContextCompactionReplayReader`（222 行）、`InMemoryResearchCatalogRepository`（210 行）、`BusinessSkillRuntime`（186 行）、`PdfSnapshotMigrationService`（183 行）、`MCPToolAdapter`（147 行）。

前端候选：58 个没有页面用到的文件，集中在 `features/admin`、`features/studio`、`features/memory`、`features/quality`、`features/topics`、`components/layout/{sidebar,topbar}.tsx`、`components/ui/{select,separator}.tsx`。`types/katex.d.ts` 是类型声明、`components/papers/__tests__/paper-pdf-viewer.layout.browser.mjs` 是浏览器测试脚本，这两个不按“没有引用”处理，单独确认用途。只被测试用到的 14 个文件（例如 `paper-reader-ops-panel.tsx` 595 行）连同测试删除。

其他：

- `scripts/graph_only_migration/`（9 个文件 5185 行）是 2026-08-25 归档的 graph-only 迁移工具，分两部分：
  - 迁移本身（`reader`、`planner`、`transformer`、`contracts`），只在读取迁移前格式的历史数据时需要。确认没有环境还保存着迁移前的数据后，连同 `tests/scripts/test_graph_only_migration.py` 删除，在 `docs/` 留一段说明，需要时从 git 历史找回。
  - 扫描工具（`import_graph`、`zero_reference_scan`、`deletion_proof`、`stale_reference_audit`），被 5 个架构测试用来保证旧编排代码不再出现。这部分挪到 `tests/architecture/support/`，继续生效。
- 根目录的 `codex-handoff-*.md` 两份交接文档移到 `docs/plans/archive/`。
- `frontend/root-env.mjs`（给 `next.config.mjs` 用）和 `frontend/root-env.ts`（给 Playwright、Vitest 配置用）是同一份逻辑的两份拷贝。只保留 `.mjs` 一份并补上类型声明，配置文件都从它导入。
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
- `backend/research/rag/evaluation/paper_benchmark_suite.py`（3759 行）在搬进来时按 5.3 的规则一起拆分。
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

重点文件的拆分方案：

| 文件 | 行数 | 拆分方向 |
| --- | --- | --- |
| `framework/harness/control_plane/harness.py` | 7276 | `HarnessControlPlane`（5766 行，114 个方法）拆成活动派发与执行输入、图初始化与决策、活动结果记录与提交、观测与运行操作、等待原因、历史校验与恢复和检查点六个协作对象，原类保留 26 个公开方法作为门面 |
| `framework/harness/control_plane/graph_application.py` | 5828 | 按决策类型拆：步骤决策、运行决策、等待登记、补偿调度、副作用结果校验 |
| `infrastructure/storage/events/postgres.py`、`sqlite.py` | 5725、4467 | 两个存储按同一组概念拆成包：事件流、订阅、投递与租约、死信与重投、隔离区、重放报告、检查点、行映射。两边 39 个同名方法里与数据库无关的校验和报告组装提到共用模块，只把 SQL 留在各自实现里 |
| `framework/harness/task_plan/parallel.py` | 4644 | `ParallelAgentCoordinator` 拆成准入与回滚控制、分组创建与恢复、派发与汇合、重启恢复与对账、取消 |
| `framework/harness/control_plane/durable_events.py` | 4380 | 事件端口、转换端口、规范化适配器、图投影记录各成模块 |
| `framework/llm/routing/router.py` | 4350 | `LLMRouter` 只有 6 个公开方法却有 3713 行：拆出全局预算预留与结算、缓存尝试、结构化输出契约、流式调用、路由指标与事件记录，路由器本身只做编排 |
| `framework/harness/control_plane/graph_evaluator.py` | 4115 | 求值器主体、分支输出合并、循环与选择汇合判断、候选筛选 |
| `framework/harness/task_plan/{replay,store,durable_store}.py` | 3953、3851、3427 | 重放：按事件类别拆 apply 函数；存储：内存实现与容量、队列、批次准入校验分开 |
| `backend/research/application/single_paper_runtime.py`、`parse_paper.py` | 3769、3432 | 用例主流程、结果模型、兼容处理器、转录与投影工具分开；解析质量报告和解析缓存身份独立成模块 |
| `interfaces/services/mcp_service.py` | 3393 | 工具、资源、提示词定义按主题拆成定义模块，服务类只做分发 |
| `framework/events/schema/catalog.py` | 3339 | 每类事件的载荷 schema 各成一个模块，目录类只负责注册 |
| `interfaces/composition/research.py` | 3207 | `_build_configured_composition`（762 行）按子系统拆成若干装配函数 |
| `interfaces/services/research_desktop_service.py` | 2992 | 服务类与报告、引用、阅读器投影函数分开 |
| `interfaces/api/app.py` | 1253 | `create_app`（451 行）里剩余的内联路由搬进 `routers/`，审计、权限、SSE 工具独立成模块 |

前端：

- `lib/i18n/translations.ts`（2195 行）按功能命名空间拆到 `lib/i18n/messages/<命名空间>.ts`，入口文件只合并。
- `components/papers/open-reader/open-reader-page.tsx`（1477 行）、`components/papers/shared/paper-pdf-viewer.tsx`（1371 行）、`features/portal/reader/reader-assistant.tsx`（874 行）：状态和副作用抽成 hook，界面拆成子组件。
- `lib/papers/real-data.ts`（1336 行）、`lib/projects/mapper.ts`（1331 行）、`features/evidence-graph/evidence-graph-data.ts`（1172 行）：按实体拆映射函数。
- `features/projects/components/projects-product-page.tsx`（1183 行）：拆成页面区块组件。

完整的超限清单由 6 节的检查脚本生成，上表以外的文件按同样方法处理。

### 5.4 合并过碎的文件

不是所有小文件都要合并。端口、错误类型、配置模型这类“一个概念一个文件”的小文件是有意的，保留。要处理的是这两种：

- **空壳实现**：类只有一行转发，例如 `framework/memory/retrieval/hybrid_strategy.py` 的 `HybridMemoryRetrievalStrategy.search` 直接返回 `store.search(query)`。没人用就删，有人用就让调用方直接用 store。
- **同一职责被切成多个几行的文件**，而且总是一起被导入。按职责合并：

候选（先按 5.1 的方法查引用：只被本包 `__init__.py` 重新导出、没有别的调用方的直接删除，例如 `keyword_strategy`、`temporal_strategy`、`consolidation_policy`、`prompts/renderer`；剩下有人用的再合并）：

| 现在 | 合并为 |
| --- | --- |
| `framework/memory/retrieval/{hybrid,keyword,temporal}_strategy.py`、`reranker.py` | `framework/memory/retrieval/strategies.py` |
| `framework/memory/stores/{graph,keyword,temporal,hybrid}.py`（都是几行的 Protocol） | `framework/memory/stores/protocols.py` |
| `framework/memory/policy/{consolidation,forgetting}_policy.py` | `framework/memory/policy/models.py` |
| `framework/memory/context/{budget,citation,compression}.py` | `framework/memory/context/models.py` |
| `framework/llm/prompts/{renderer,variables}.py` | `framework/llm/prompts/rendering.py` |
| `backend/foundation/feedback/{feedback_collector,feedback_aggregator}.py` | `backend/foundation/feedback/pipeline.py` |

合并后原路径不保留转发模块，调用方一次改完。完整候选清单由 6 节的检查脚本列出（≤ 15 行的非 `__init__` 模块），逐个按上面两条判断。

### 5.5 生产目录里的测试替身和假数据

- 17 个 `fake.py`、`testing.py`、`fake_store.py`、`fixtures.py`（2793 行）挪到 `tests/support/`，目录结构对应原包。其中有 3 个被生产代码引用（`framework/harness/side_effects/fake.py`、`framework/harness/context/fake.py`、`framework/harness/workers/fake.py`），要先看引用方：是冒烟命令（`agent_loop_smoke_service`）用的，就给冒烟命令一个明确的离线实现并起正式名字；是装配时的默认值，就改成必须显式传入。
- 前端 `lib/mock-data.ts`（966 行）和 `lib/api/mock-data.ts` 只给测试和 `NEXT_PUBLIC_NEWSROOM_USE_MOCKS=true` 的开发模式用。按项目规则“项目代码用真实数据”，删除 `lib/api/client.ts` 里的 `shouldUseMock` 假数据模式（没有部署配置用到这个变量），假数据挪到 `src/test/fixtures/` 只给测试用。下面这些在接口没数据时回退到假数据的地方改成显示空状态或错误状态：`features/sources/hooks/use-sources.ts`、`features/memory/hooks/use-memory-search.ts`、`features/quality/hooks/use-quality-results.ts`、`features/studio/artifacts/hooks/use-artifacts.ts`、`features/tech/hooks/use-tech-items.ts`、`features/topics/**`、`lib/search.ts`。`features/admin/lib/mock-data.ts` 里的导航配置不是假数据，改名为 `navigation.ts`。
- `lib/papers/taxonomy-fallback.ts` 只被测试用到，确认后删除。
### 5.6 精简包的导出表

`__init__.py` 合计 16373 行。`framework/harness/__init__.py` 有 1937 行、622 个懒加载导出项，`framework/events/__init__.py` 有 1093 行。包内部代码已经直接从定义模块导入，包外只有 21 处从 `framework.harness` 根导入、18 处从 `framework.events` 根导入。

规则：

- 只有层的公开入口保留导出表（例如 `framework.harness`、`framework.events`、`framework.llm`），只列包外确实在用的名字。
- 其他子包的 `__init__.py` 不写导出表，只写一行模块说明。
- 包外的导入改成从公开入口或定义模块导入，二选一，同一个名字全仓库只有一种写法。
- `__init__.py` 上限 150 行，纳入 6 节的检查。
- 现有 `tests/architecture/test_framework_export_surfaces.py`、`test_framework_public_api.py` 按新的导出表更新。

### 5.7 依赖路径的架构测试

`tests/architecture/` 有 77 个架构测试，其中不少按文件路径登记（`fixtures/graph_only_freeze_baseline.json` 112 个路径，`fixtures/harness_graph_caller_inventory.json` 144 个路径）。拆分和移动文件后，这些清单要用各自的生成脚本重新生成，不能手改；生成后检查差异只有路径变化，没有新增调用方。

## 6. 防止回退的检查

新增 `tests/architecture/test_codebase_hygiene.py` 和前端对应的检查脚本，进 CI：

| 检查 | 内容 |
| --- | --- |
| 文件大小 | 源文件、`__init__.py`、测试文件按 5.3、5.6 的上限检查。整理期间允许一份 `tests/architecture/fixtures/oversized_files.json` 清单，只能删条目不能加，阶段 4 结束时清空 |
| 评测依赖方向 | `framework`、`backend`、`infrastructure`、`interfaces` 不得导入 `evaluation` |
| 测试替身位置 | 业务包里不得出现 `fake*.py`、`testing.py`、`fixtures.py`；前端 `src/` 里除 `src/test/` 外不得导入 `fixtures` |
| 死代码 | Python 用 vulture 扫描四个业务包和 `evaluation`（置信度 80 以上报错，误报写进白名单并注明原因）；前端用 knip 扫描没有引用的文件和导出 |
| 重复文件 | `frontend/root-env` 这类同名不同扩展的拷贝只允许一份 |

vulture 和 knip 加进开发依赖，CI 的检查命令统一走 `python -m scripts.dev hygiene` 和 `npm --prefix frontend run hygiene`。

## 7. 实施步骤

每一步单独提交、单独跑全部测试，任何一步都能单独回退。步骤之间按顺序进行，前一步合进 main 再开始下一步，避免长期分支冲突。

| 步骤 | 内容 | 验证 |
| --- | --- | --- |
| 0 | 建立基线：跑全部测试，记录结果；把附录 A 的统计脚本放进 `scripts/`；加上 6 节的检查和超限清单（先只记录，不失败） | 全部测试通过，统计数字与第 4 节一致 |
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
| 和并行开发的分支冲突（例如下一个任务“模型连接整合”） | 拆分超大文件的步骤 4 安排在模型连接整合合并之后，或先和该任务约定不碰的文件 |
| 前端去掉假数据回退后，开发模式下部分页面变空 | 这是预期结果，页面显示明确的空状态；需要演示数据时用真实接口导入真实数据 |

## 附录 A：统计方法

- 文件行数：`git ls-files` 列出的源文件逐个计行，测试按路径（`tests/`、`__tests__/`、`*.test.*`、`*.spec.*`、`e2e/`）区分。
- Python 依赖图：用 `ast` 解析全部模块的 `import`、`from ... import` 和形如 `"包.模块"` 的字符串引用，从 `interfaces` 的 CLI、API、MCP、worker 入口出发求可达模块；从测试、`scripts/`、`data/`、`examples/`、`sdk/` 出发分别求可达集合，用来区分“没有引用”“只有脚本用”“只有测试用”。
- Python 顶层符号：一个模块里的顶层类或函数，在本模块内没有被引用，并且在其他业务模块、配置文件里都不出现这个名字，计为没有引用。
- 前端依赖图：解析 `import`、`export ... from`、动态 `import()` 和 `vi.mock`，从 Next 的 `page`、`layout`、`route` 等约定文件和 `proxy.ts`、配置文件出发求可达文件。

## 附录 B：整理后的统计

（步骤 6 完成后填写）
