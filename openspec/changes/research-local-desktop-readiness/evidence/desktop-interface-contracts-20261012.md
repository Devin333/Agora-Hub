# Desktop 接口与权威合同

记录日期：2026-10-12。状态：2026-10-13 已完成 backend/frontend/runtime 三方最终静态核对；task 1.7 已核销。该文件不表示测试或真实验收通过。

## 配置与身份

profile 为 default 或 local-desktop，未知非空值应拒绝。本机启动工具只使用 local-desktop；配置由 CLI > runtime config > 原根 environment > defaults 解析。canonical browser origin 与 frontend bind/port、API base 与 API bind/port 必须一致。NEWSROOM_CANONICAL_ORIGIN 和 NEWSROOM_DESKTOP_CANONICAL_ORIGIN 若同时出现必须相同；单一解析器输出统一下游值。Node/npm 与 .venv/Scripts/python.exe 固定身份，release 不从可变 cwd 推导用户数据位置。

浏览器使用 newsroom_session HttpOnly/SameSite cookie。BFF 从 cookie 提取会话、在已验证同源写请求中添加 canonical Origin 和 X-Newsroom-Session。API 根据真实 AuthApplicationService session 创建 user actor，并从可信 actor metadata 绑定 user/tenant/memory namespace；任何 browser scope 字段应拒绝。bootstrap/login 无先前会话，但受 canonical origin、严格输入、默认有界限流和初始化状态限制。direct API、rewrite 和 BFF 的私有路径统一授权。local-desktop 旧执行 mutation 应明确禁用或转到同一 admission，不允许绕 options/恢复合同；非本机原合同保留。

## 实际路由族

| 浏览器 BFF | API | 含义 |
| --- | --- | --- |
| GET /api/research/desktop/identity | GET /api/v1/research/desktop/identity | trusted owner、generation、requestContract |
| GET /api/research/desktop/capabilities | GET /api/v1/research/desktop/capabilities | 配置/可达/验证的确定性投影；未探测不是网络失败 |
| GET /api/research/desktop/papers/{paperId}/reader | GET /api/v1/research/desktop/papers/{paperId}/reader | scoped 正文、真实 source identity、ask/analyze 资格 |
| POST /api/research/desktop/operations/analyze | POST /api/v1/research/desktop/operations/analyze | 单论文分析，固定语义，bounded Harness |
| POST /api/research/desktop/operations/ask | POST /api/v1/research/desktop/operations/ask | exact accepted run 与证据范围的真实 grounded 问答 |
| POST /api/research/imports | POST /api/v1/research/desktop/operations/import | authenticated PDF bytes + 首次发送前 durable request identity |
| GET /api/research/desktop/operations/{requestId}?generation=... | 对应 API 路径 | 首回执丢失时仍可核对原业务身份，不重发 |
| GET/POST /api/research/desktop/reports | 对应 API 路径 | 读取 owner saved refs / 保存已接受 run 的结果引用 |
| GET /api/research/desktop/reports/{reportId} | 对应 API 路径 | saved reference + 原 exact-run report projection |
| GET /api/research/desktop/reports/runs/{runId}?sourceRevision=... | 对应 API 路径 | 精确运行报告，不用 paper 最新结果替代 |
| GET exact-run report /export?...&format=markdown 或 json | 对应 API 路径 | application 校验 owner、source、artifact 和 accepted export 资格后下载 |
| POST /api/research/desktop/reports/drafts/export | 对应 API 路径 | 用户草稿独立导出，明确 draft_unverified |

private PDF bytes/资产的最终受控 URL 由原 imports/artifact owner 提供，并补入最终合同；内部 file URI/绝对路径不能返回浏览器。

## 请求语义与状态

requestId 在首次发送前创建，owner-scoped 唯一、稳定，服务端 durable 原子 admission；generation 形如 gen_ 加 32 位小写十六进制。requestFingerprint 和 sourceFingerprint 为 sha256: 加 64 位小写十六进制。完整固定语义保存于原 owned workspace；语义样例见 request-semantics-contract-fixtures.json。fingerprint 用 UTF-8、Unicode ordinal key sort、compact JSON、明确 null 字段，不使用 localeCompare 或随恢复改变内容。

analysis semantic: kind、paperId、sourceRevision、sourceFingerprint、sourceUrl、pdfUrl、objective、constraints。ask semantic: kind、runId、paperId、sourceRevision、sourceFingerprint、question、selection、locale。import semantic: kind、规范化原 filename、sizeBytes、sourceRevision、sourceFingerprint；bytes 本身不写 workspace，但实际 bytes checksum/size 在服务端绑定。中文 X-Filename 使用一次 percent encoding/decoding，与语义规范化一致。kind 用于语义 fingerprint；transport body 按 route 的明确 allowlist 构造，不将额外 kind 或任意 options 混入严格请求模型。

admission 保存原 run/import/answer 身份，原 executor/run record/transcript/artifact 决定业务结果。UI DTO status 为 waiting/running/needs_action/passed/rejected/failed/interrupted/unknown；prepared 只表示尚未发送的客户端 durable intent。原 metadata 状态到 UI 的投影不得将未确定或仅请求结束当通过。get_operation 核对原业务权威并可恢复已执行但 complete 回执缺失的情况。相同身份同语义复用，不同语义 409；旧 generation 拒绝，404 不允许自动再执行；用户明确新执行需新身份。browser abort 仅停止等待，不显示已取消。

## Workspace 扩展

原 ResearchHistoryService 的 owner/revision/CAS store 仍是工作区权威。readerNotebooks 绑定 paperId/sourceRevision、note/question/font/section/page；pendingOperations 绑定 generation/requestId/fingerprint、完整 typed semantic 和原 run/import 身份；savedReports 保存引用或草稿关联。长期 selection/events 必须进入该 owner 的 typed bounded 扩展，不能留在无 owner localStorage 或已移除的 legacy API。guest/legacy 显式导入、双标签冲突、未同步输入保留和 owner 切换保持同一 CAS 语义；服务端回执才称账号已保存。

## Reader 与报告

reader ready 需要真实通过资格的正文与 exact source identity；needs_review/unavailable 允许缺字段，但缺身份不能分析/问答、不能用 hash(paperId) 冒充源 checksum。私有资料从 authenticated application 提供，不以公共缓存作为私有资格。来源修订变化的章节/页定位必须失效提示，不能假装精确续读。

Research report 是 application 的原结果投影，形状为 schema/runId/paperId/sourceRevision/status/disposition/analysis/quality/citations/artifactRefs/traceRef/diagnostics/eligibility。save POST 与 saved GET 返回 reference/report；list 返回 reports 数组中的 saved references。frontend strict schema 验证后做纯展示投影，不改变质量或发布语义。

analysis 来源为 ResearchAnalysis：summary 是 ThreeMinuteRead 对象（problem/core_idea/method_summary/experiment_summary/key_contributions/limitations/why_it_matters/read_next/evidence_refs），另有 contributions/methods/experiments/limitations/reproducibility/related_work/claims。quality 为 ResearchQualityResult：passed/score/gate_results/quality_flags。展示或 Markdown 导出不能假设 summary 是字符串或 quality 有 status 字段。

eligibility.accepted/qualityPassed/acceptedExport 由原 deterministic gate、disposition 与 artifact 校验得到。展示、保存与导出核对同一 run/paper/source identity、artifact type/checksum/payload 一致性；引用从实际 EvidenceRef/span/document locator 解析，找不到的定位明确 unavailable，不能安全 HTTP ref 就一律 valid。内部路径、prompt、server credentials 和任意 metadata 不透传。

## 停写与恢复控制

control authority 位于原 request-admission/auth 边界，默认 control path 各层一致。runtime 通过 application adapter begin_drain/resume_admission/rotate_generation 操作，CLI 不创造业务状态或改 accepted records。停写覆盖每个实际 writer，active 工作按有界生命周期收敛；异常退出的 operation 待核对，不永久伪装 running。

恢复映射真实 owner roots，schema 与 release compatibility 同一来源；原 owner 允许的未受保护 path metadata 才可重定位，受 checksum 保护的 payload 不能任意重写。staging application 仅绑定恢复根并核对 owner/refs/index/实际资料；会话撤销、新 generation fencing 旧 intent，不自动重放。多 owner activation journal 全部路径预检后才移动，恢复中断保持原一致数据，rollback 先验证目标与兼容备份。

最终源文件/DTO 摘要、三方确认与 task 1.7 完成时间在合同冻结时追加。第 11/12 章证据独立，不借用本静态合同宣称通过。

## 三方接线补充（冻结前）

- 私有 PDF 的浏览器资源入口为 GET `/api/v1/research/desktop/papers/{paperId}/source?sourceRevision=...&sourceFingerprint=...`，由同源 rewrite 进入 session/owner 受控 application，返回 private/no-store、nosniff 的 PDF。该受控 URL 仅是展示地址，不能当作新的远程来源输入。analyze semantic 中私有资源的 sourceUrl/pdfUrl 为 null，由后端依 owner 与 exact source identity 解析原 import。
- saved report 的当前服务端合同为 GET `/api/v1/research/desktop/reports/{reportId}`，前端不得自行增加 `/saved/`。真实 URL 必须在冻结时由两端实际 router/BFF 源文件确认。
- 解析正文版本与上游元数据版本是两类身份。已解析资料进入分析时，原 source provider port 应绑定 actor-visible document 与正文 source hash，并保留真实源记录；不能以 arXiv 元数据摘要代替 PDF checksum，也不能仅复制客户端 fingerprint 作为已核验身份。
- 问答审计由原 PaperRagTranscriptFileStore 同一 owner 根保存。配置为 NEWS_RESEARCH_RAG_TRANSCRIPT_ROOT，默认为 research_root/rag/transcripts，schema 为 newsroom.paper_rag_transcript.v2；逐事件 journal 与最终 transcript 同时参与 backup/restore 与 checksum/scope 校验。新请求与原 request/session physical identity 绑定，查询不重放。
- 桌面入口通过显式 desktop-entry status/install/remove 管理当前用户的实例专属快捷方式。autostart 仍默认关闭，其启用/移除不能覆盖其他实例或用户已有入口。
- 浏览器 fixture 回归只属于第 11 章；真实来源/provider/PDF/重启/恢复仍需第 12 章独立执行。

以上补充仍为最终实现静态核对中的合同，不代表所有实现已收尾或检查已执行。

## 运行身份与恢复投影补充

operation DTO 的 `recoverable: true` 与 `automaticRetryAllowed: false` 在服务端原 admission 投影、前端严格 schema 和首次发送前的视图保持一致。状态查询只核对原 request/generation，不自动创建新任务。

local-desktop 生产进程的运行身份为 `{deployment_profile, instance_key, release_id, config_digest, boot_id}`，其中 profile 必须为 local-desktop，config digest 为 64 位小写十六进制，boot id 为 32 位小写十六进制。由已验证 release 的启动环境分别注入 API 和 frontend 进程，经 health 与 auth/session 的 `data.local_desktop` 返回；缺失、非法或与本次启动不符不满足 readiness。default profile 保留既有合同。

Research 与 auth 的实际 writer 共用原 application 停写控制，不将匿名认证入口例外误解为 drain 例外。快照和激活必须在可证明的一致性边界内进行。


## 公开正文准备补充（实现待三方冻结）

公开来源进入本机 owned reader 前必须显式准备正文。后端当前路由为 POST `/api/v1/research/desktop/operations/prepare`，语义字段为 kind=`prepare`、paperId、sourceUrl、sourceType（允许 null）；同样先保存 owner-scoped requestId/generation/完整语义，再绑定原 parse run 后执行。GET reader 不触发付费或解析副作用，未解析正文不能以外部 reader 元数据的 hash 代替本机解析内容身份。prepare 成功回执应提供原 application 解析出的正文身份，前端刷新真实 reader 后才允许用户显式分析；失败与回执丢失进入既有待核对合同，禁止盲重试。最终请求模型、DTO 和共享 Unicode fingerprint fixture 由两端实际实现确认。


## 最终静态对齐要点（2026-10-13）

当前两端 canonical JSON 都按 UTF-16 code unit ordinal 顺序排序 key；Python 使用 utf-16-be 排序，JavaScript 使用明确的 `<`/`>` 比较，不使用 localeCompare。仅规范 JSON 值参与语义，显式 null 不被省略；真实 workspace round trip 覆盖全部五个共享语义样例。prepare 的 immutable semantic 不包含解析后才取得的 revision，结果身份存在原 operation result 与真实 reader 中。

prepare immediate response 和恢复均读取原 parse final（runId、actor、request fingerprint），并核对 original document_id/source hash/quality receipt；不存在终态时不由 catalog latest document 推测完成。analysis 使用实际 bound document 身份和 publication 前核验。ask 正常与恢复路径都拒绝 abstained/empty answer，坏 journal 与中断不授予自动 retry。

公开 report analysis/quality 采用显式字段投影并映射 evidence_refs 为 evidenceIds；summary/claim/quality gate/flag 均为严格 typed 内容。原 domain 必需 paper_id/summary/evidence_pack_id 与 quality result 需完整，accepted/qualityPassed/export 资格不能从 sparse payload 推测。引用资格只来自持久化 document/evidence 中的实际 section/page/span。


## 静态合同冻结（2026-10-13）

三方 owner 已交接且没有确认的未实现项。主控核对严格请求 DTO、UTF-16 ordinal 共享五组语义、session/actor 权威、exact reader identity、prepare/import/analyze/ask admission、终态恢复与公开报告投影后冻结本合同。第 1–10 章的代码与测试代码核销完毕，候选 source/dependency 清单见 candidate-freeze-before-validation-20261013.json。上文“待冻结/待修复”描述保留为审查历史，最终状态以本节与三个 owner handoff 为准。集中测试尚未开始，不能将此记录作为运行通过或 live 验收证据。
