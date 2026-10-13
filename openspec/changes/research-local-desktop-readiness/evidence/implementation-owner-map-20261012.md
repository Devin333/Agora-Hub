# 实现基线与所有权映射

记录日期：2026-10-12；对应任务 1.1–1.6、1.8。数据来自当前文件静态阅读、版本命令及命名配置键解析；未运行应用、测试、构建、compile、smoke 或真实 provider probe。

## 版本与配置

开始实现时 HEAD 为 `4d867043d060b30ebf14bc12f738a90a9a574fa4`，工作树干净。解释器/Node/npm、14 个实际源文件 SHA-256 和解析后存储路径见 `implementation-baseline-20261012.json`。该文件只保留命名 credential 的存在性，不保留秘密值。源文件摘要用于定位阅读版本，后续实现的最终候选应重新绑定身份。

## 入口和迁移合同

| 当前入口 | 当前实现 | 本机交付目标 |
| --- | --- | --- |
| `/` | PortalHomePage，读取 Portal 数据 | local-desktop 使用正式共用 Research 首页；其他 profile 保留原语义 |
| `/design-demo` | DesignDemoPage | 本 profile 限定 redirect 到 `/`，保留合法 query/恢复定位 |
| `/design-demo/papers` | PapersDesignDemoPage + 真实 public cache | `/papers`，保留 taxonomy/已有子页面 |
| `/design-demo/papers/[slug]/read` | PaperReaderWorkspace | `/papers/[slug]/read`，保持 source revision 与返回路径 |
| `/papers/[slug]/read` | PaperDocumentReaderPageClient | 按 profile 使用统一正式 reader，不建第二套状态 |
| `/design-demo/groups/[groupId]` | 既有分组详情 | `/research/groups/[groupId]` |
| `/reports` | 草稿准备与既有报告列表 | 草稿与 Research accepted projection 明确区分 |
| `/login` 与 `/api/auth/*` | first-user bootstrap + username/password | 正式开启；认证入口匿名例外受 origin/输入/限流/初始化状态限制 |

`frontend/src/lib/research/entry.ts` 和 history validators 是深链接/历史规范化的共有入口。旧路由仅允许本变更定义的迁移，不将任意 returnTo 变成开放 redirect。新记录写正式路径，旧记录在 owner-scoped CAS 下幂等迁移。

## 应用边界与真实缺口

| 内容 | 原 owner / 接线 | 已核查状态与必要补齐 |
| --- | --- | --- |
| 账号 | AuthApplicationService，LocalJsonAuthUserRepository/SessionRepository | 真实口令账号已有；desktop profile/API guard、session→Research actor 接线需补齐 |
| 工作区 | ResearchHistoryService；revision/CAS；frontend history/history-sync | materials/reportDrafts/prompts/composerDraft/activity/conversation 已有，不能重新建 workspace DB |
| 笔记与 reader 设置 | useReaderNotebook | 目前无 owner localStorage；必须接入原 owned workspace 的 typed 扩展，legacy 内容显式导入 |
| 旧 notes/selections BFF | `/api/papers/[id]/notes` 等 | 转发到移除的 `/api/v1/papers`；不是本次可用后端，禁止重建 legacy compatibility router |
| 分析 | ResearchApplicationService.analyze_paper → AnalyzePaperRequest → 原 Harness runtime | 已有真实路径，新增 admission/request identity 与 UI 接线，不复制 executor |
| 问答 | ResearchApplicationService.ask_paper / 原 RAG application | 以可信 owner 与 accepted paper/evidence 为准，可选 RAG 缺依赖不冒充可用 |
| 结果 | 原 ResearchRunStore / artifact port / quality | get_analysis(paperId) 读最新 accepted record；特定 run 报告须增加原 service 的按 run 投影，不混入最新其他结果 |
| 运行诊断 | get_trace(runId) | scoped run/diagnostic/reconciliation 已有；新增 UI 状态投影必须复用这些权威数据 |
| 导入 | ResearchImportService → ResearchImportApplication.process → ResearchParseInput | PDF bytes/index 与解析已独立拥有；request 需首次发送前落盘并在服务端绑定，首回执丢失也可查 |
| 报告 | 通用 ReportApplicationService 与 ResearchAnalysisResult | 不同模型/owner；desktop 通过 application projection 读取原结果，不写第二个 report truth store |
| 运行工具 | CLI api serve、npm scripts | 复用实际入口；新增 loopback profile、production release、owned lifecycle 与恢复工具 |

## 现有 API / DTO 合同

| API | 当前调用与含义 |
| --- | --- |
| POST `/api/v1/research/guided/intent`、`guided/search` | GuidedResearchService；候选意图与真实 source search，不是完整分析结果 |
| POST `/api/v1/research/imports`、GET imports/id、POST imports/id/retry | authenticated user、真实 PDF 接收/转换；当前返回后才有 importId 的问题需补齐 |
| POST `/api/v1/research/papers/parse` | ResearchParseInput，经 Research application 进入既有 parse pipeline |
| POST `/api/v1/research/papers/analyze` | paperId/sourceUrl/pdfUrl/options/runId 等，actor 不能来自不可信 browser scope |
| GET papers/id/analysis、reader、document、sources、catalog | 原 application service 的 scoped/public rules；私有 imports 不能依赖 public-cache-only guard |
| POST papers/id/ask、rag-ask | question/selection/options；只有允许字段能进入 runtime |
| GET `/api/v1/research/runs/{run_id}/trace` | 原 run store/diagnostic/reconciler；readiness、pending UI 不得直接改此业务状态 |

新 desktop DTO/路由与 generation/恢复 API 的最终签名由对应 backend/frontend/runtime 集成后补入单独合同文件；1.7 在合同冻结前保持未完成。

## 必需与可选能力

| 能力 | 本版级别 | 资格与失败边界 |
| --- | --- | --- |
| 本地账号、owned workspace、CAS | 必需 | service 回执才叫保存，直接 API 必须授权 |
| 真实来源搜索/引用解析 | 必需 | actual adapter/source；空结果/来源拒绝不填 fixture |
| 一份真实 PDF 导入与阅读 | 必需 | 原 source/parse/publication path；缺 parser 最终验收不能勾选 |
| 真实单论文分析、论文问答 | 必需 | configured provider、bounded runtime、accepted evidence 与 gate |
| 报告保存/引用/Markdown+JSON | 必需 | 特定 run 与 source revision，owner/完整性/quality 校验 |
| OCR 扫描件 | 可选 | 只有所选 parser 确实支持并验证才开放，不把文本 PDF 成功扩展成扫描件承诺 |
| Docker parser isolation | 按解析路径必需 | CLI 存在不代表 engine/image/model 可用，缺能力不绕到宿主机 |
| dense/RAG/embedding | 按选定功能可选 | 缺依赖只禁用相应入口，不悄悄改成未准入查询实验 |
| 公共 OTP/OAuth | 不作为本机前置 | 保留真实配置状态，不 fake 登录 |
| 并行/候选检索/P2 continuity | 原 owner 条件控制 | 不随本机交付扩大启用 |

能力 configured/reachable/verified 分开记录。静态配置存在性只证明 configured 的一部分，本文件不声称真实 provider/解析已通过。

## 存储与一致性清单

解析路径见 JSON baseline；以下是来源与一致性责任，backup 需继续纳入新增 admission 元数据。

| Owner | 配置 / 默认 | 一致性与引用 |
| --- | --- | --- |
| auth users/sessions | NEWSROOM_AUTH_USERS_PATH / NEWSROOM_AUTH_SESSIONS_PATH；`.newsroom/auth/*.json` | locked_json_file；恢复后 session 必须失效 |
| workspace/history | NEWSROOM_RESEARCH_HISTORY_PATH；`.newsroom/research/history.json` | owner + revision CAS + locked JSON；typed 扩展的笔记同 owner |
| imports | NEWSROOM_RESEARCH_IMPORT_DIR；`.newsroom/research/imports` | index + uploaded bytes + processing lock；index 的绝对 path 必须安全映射到恢复根 |
| Research artifact | NEWS_RESEARCH_ARTIFACT_ROOT / NEWS_ARTIFACT_ROOT；`.newsroom/runs` | 原 artifact checksum、run refs、Graph lifecycle manifest；不任意重写受保护 payload |
| Research records/latest | NEWS_RESEARCH_RUN_STORE_ROOT；research_root/runs | filesystem record/index lock、schema/checksum/actor scope；恢复引用必须一致 |
| Harness Wait | run_store_root/harness-waits | 原 durable Wait owner，不能漏备份或把未决状态当成功 |
| local chunks | NEWS_RESEARCH_RAG_LOCAL_ROOT；research_root/chunks | 原 chunk collection/index identity；可重建与必需引用按真实 config 判断 |
| events/replay checkpoints | durable_event_storage_from_env；artifact_root 下 SQLite 或 NEWS_DATABASE_DSN | SQLite/WAL 一致快照；外置 PostgreSQL 不支持快照时明确拒绝，不称单目录备份完整 |
| public paper cache | NEWSROOM_PAPERS_DATA_PATH 或现有可用 shared cache | 明确 public/cache 身份；可重建项可排除但需列明外置依赖 |
| desktop control/releases/logs | 后续运行工具 | 只 operational metadata；不能复制业务 run store；秘密/内容日志/release依赖与大模型默认排除备份 |

首版采用停写/停服务快照。隔离恢复校验不能读取原业务数据根；原 owner 不支持安全重定位的引用必须拒绝。恢复的新代际 fencing 旧请求，not-found 不授予再次外部执行许可。

## 任务与证据所有权

- `research-workspace-efficiency` 5.2/5.3 继续属于原 change，只有同一最终候选真实 browser/required checks 证据逐项满足后分别核销。
- `research-evidence-context-quality-foundation` 的 gate/输入/证据服务被复用；检索 rollout 与 P2 trigger 不由本 change 决定。
- `harness-runtime-execution-and-parallel-agent-orchestration` 的 provider/release/rehearsal 资格、`durable-event-runtime` 9.5 deployment drill、model-aware preflight 未闭环验证均不因 desktop 本地检查自动完成。
- 第 11 章之前只有实现与测试代码，不执行测试；第 12 章的 live 成功流程不使用 route mock/fixture，不擅自重启电脑。
