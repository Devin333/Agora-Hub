# 静态集成审查记录

日期：2026-10-12；状态：实施中。此记录描述实现阶段发现、已交由对应 owner 修复的合同问题；记录本身不表示代码或验收已通过。最终候选需重新静态核对并绑定第 11/12 章执行证据。

| 关注点 | 必须满足的最终合同 | 所属实现 |
| --- | --- | --- |
| profile 与 origin | 严格枚举，单一配置解析；custom port 的 browser/cookie/BFF/API Origin 完全一致，未知值拒绝 | shared/local_desktop、frontend deployment-profile、desktop auth |
| 浏览器身份 | 原会话经服务端绑定 actor；private raw API/rewrite/BFF 都无法绕过；bootstrap/login 匿名例外仍受同源/输入/限流/初始化约束 | auth application、API middleware、BFF |
| 首次发送与指纹 | 首次网络请求前按 owner durable 保存 requestId/generation/完整固定语义，Unicode ordinal canonical JSON 和 sha256: 摘要跨语言一致 | workspace、pending-operations、request-admission port |
| DTO | 同一实际路由/字段/状态/空值合同；UI 状态投影以 admission 与原业务权威为准，不能从 waiting 推测通过 | desktop router/service、frontend contracts/client |
| 崩溃与回执丢失 | bind original run/import/answer identity before effects；查询原 record/transcript/artifact，未确定保留待核对；旧代际、404、browser abort 不允许自动再执行 | Research application/admission、UI recovery |
| 精确来源与报告 | runId 和 source revision/fingerprint 必须由原结果证明；缺身份、损坏 artifact、质量拒绝不能作为 accepted report | ResearchApplicationService exact-run projection |
| 真实问答 | 既有关键词摘要回答不足以通过 AC-08；复用 bounded Harness RAG session/answer worker 与 deterministic gate，不要求未启用 dense 实验 | 原 application/port、production composition |
| reader 与导入 | private PDF/HTML 经 authenticated application 可读；不依赖 public cache 作为私有资格；不把分析片段伪装全文 | 原 document/artifact owner、desktop reader |
| 能力与 probe | configured/reachable/verified 分开；一般 OK completion/health check 不证明业务成功；实际 timeout/request/token/cost 限额，未知成本拒绝模型探测 | doctor 与 application capability projection |
| 原配置与数据根 | release 运行不依赖可变 cwd；从原根配置解析并绝对化每个真实 owner root，provider/server credentials 只进入 server env | runtime config/release/processes |
| release 身份 | frontend source/build/lock、backend resources、Node/Python/deps/config 身份可验证；缺资源、额外文件或依赖漂移拒绝 | ReleaseManager |
| lifecycle | admission drain/active lease 由原 authority 持有；进程 creation/executable/child ownership 可验证；新 start 恢复 admission，异常只收敛自有资源 | 原 control service、LifecycleManager |
| 日志 | 写盘前脱敏并有界轮换；失败不能泄漏 token、会话、prompt 或内部上游正文；collector 属于本实例进程 | operational log collector |
| 完整 inventory | 真实 auth/history/imports、Research artifact/records/chunks、Harness waits、events/WAL 与必要 index 都按配置优先级纳入 | storage inventory adapters |
| 恢复真实性 | 有界 containment、原 owner 一致性、ACL；独立配置只读恢复根，验证 schema/owner/run/artifact/index；支持实际安全路径重定位，其余引用拒绝 | BackupManager/RestoreManager 与原 application adapters |
| fencing 与 journal | session 撤销、新代际、非终态仅待核对；多 owner activation journal 支持中断回退、原资料不丢失 | 原 control/auth metadata + restore activation |
| rollback 合同 | release readable/writable schema 与真实 inventory 同一来源；兼容保数据切换，不兼容拒绝并要求已验证兼容备份 | release/recovery schema registry |
| 交付质量 | 可读代码、严格 DTO、错误反馈与输入保留；1–10 章全部实现才开始集中检查 | 全部 owner |

此表用于主控整合，不授权跳过功能、降低断言或修改既有 Harness/quality/publication 权限。最终任务勾选须有对应 source 和静态审查证据；第 11/12 章只在实际执行后更新。

## 最终冻结前仍在处理的实现证据

下列项目已交由对应实现 owner 收尾；不作为失败测试或通过测试记录。

| 范围 | 静态发现与完成条件 | 主要源文件 |
| --- | --- | --- |
| 真实问答审计 | 原 RAG controller 的事件仅内存累积，结束时才写 transcript；需要每个执行/校验事件先 durable 记录，中断后按原 request/session 读取，不吞掉持久化失败 | framework/harness/rag/session.py、backend/research/application/paper_rag_session.py、interfaces/services/paper_rag_transcript_store.py |
| 私有 PDF 分析 | upload 的 owner/source identity 已由原 import service 绑定；前端不得再要求公开 URL，受控 PDF URL 只允许已定义的同源 authenticated source endpoint | lib/research/workspace-items.ts、desktop/request-contracts.ts、research_import_service.py |
| 私有 PDF 完整性 | 完成的 import index 不能代替现有 PDF 内容校验；原 owner service 应在读取/再次分析前核对 bounded bytes、checksum 和 owner 目录 | research_import_service.py、research_desktop_service.py |
| 报告一致性 | 每个 required artifact 必须匹配自身类型、run/paper 和原 analysis/quality payload；错误类型不能被另一 required artifact 代替 | research_service.py |
| 其他 API 入口 | 本机 profile 的 session guard 必须覆盖可间接访问 Research 原 run/artifact 的 operator 路径；无 owner 检查的通用 inspection 不能直接对本机用户开放 | interfaces/api/app.py、routers/runs.py、research_desktop_auth.py |
| 能力收据 | 实际业务验证需绑定当前 generation 及 config/release/dependency identity，配置或模型改变后旧验证不能继续称当前已验证 | research_desktop_control.py、research_desktop_service.py |
| 隔离恢复 | JSON/schema 解析或 original_roots_used 字段不足以证明恢复；必须从隔离 owner 配置读取真实 workspace/run/artifact/index 并核对引用 | scripts/local_desktop/recovery.py |
| 测试资源 | 两端请求 fingerprint 负例使用稳定 tests/fixtures 文件；归档 OpenSpec change 不得破坏测试路径；browser 合同代码随实现准备 | tests/fixtures、frontend/tests/e2e |

冻结时应逐项将剩余条件对照实际 source 与测试代码核销。全量验证与实机证据仍只属于第 11/12 章。

## 接口冻结前第二次静态核对

以下为源码阅读发现，不是运行结果。

- operation application 投影与 frontend strict schema 共同保留 `recoverable: true` 和 `automaticRetryAllowed: false`。这两个字段只声明可核对原请求、不授予自动重发许可；`needs_action/interrupted/unknown` 不能触发付费重放。
- capability application 投影实际包含非敏感 `provenance` 验证收据，frontend 必须采用一致的 bounded typed schema。旧 generation 或 config/release/model identity 的记录不能作为当前 verified。
- readiness 要求 API health、API auth/session 与 frontend BFF 的 `data.local_desktop` 精确匹配当前 instance/release/config/boot identity。BFF 使用自身服务端配置，不能单凭上游声明证明 frontend 身份。
- auth bootstrap/login/logout 和可能更新 session 的读取也属于业务 writer，必须参与原 application drain/fencing。备份不得仅依据 instance.json 的 STOPPED 字段判断跨 owner 数据已一致；原 control 停写必须覆盖快照或恢复激活全程。

上述各项已经交给相应实现 owner。task 1.7 仍等待最终实现静态核对，11/12 章尚未执行。

## 报告与问答展示合同的实际差异

源码静态审查发现：application 的 desktop public projection 会剥离任意 metadata/内部 locator，并将 evidence_refs 转为 opaque evidenceIds；frontend 的严格 schema 仍按原 domain payload 要求 metadata/evidence_refs，双方目前不一致。实现 owner 正在共同固定明确 public DTO，并以实际投影形状准备必要合同测试。不能关闭 strict、透传任意 metadata 或把所有私有引用置为不可用来完成任务。

问答原输出还只保留 opaque evidence ID，缺少可回跳的可信 section/page；原 sourceRefs 中的 file URI 不得进入 browser。问答与报告需从真实 document/evidence 匹配 locator 后生成公开引用，错误 section/page 明确 unavailable。拒答、预算耗尽或 gate failure 不得被无条件投影为 passed 或 verified。

另已记录 nullable objective 在默认值处理前后导致 canonical fingerprint 不一致的实际代码路径，服务端须区分持久化请求语义与 Harness 的 effective objective。

## Windows 进程检查与停止语义

已核对项目 Python 3.12.7 对应 CPython primary source，并记录版本/source checksum 与 `os_kill_impl` 实际片段于 `windows-process-liveness-static-reference-20261012.json`。Windows `os.kill(pid, 0)` 不属于无副作用的存活查询：先尝试 console control event，失败可能进入终止进程分支。原 admission liveness 已交由 backend 实现 owner 改为只读 Win32 HANDLE 查询及创建时间身份。runtime 的 graceful stop 也直接使用明确 Win32 API，失败不能隐式进入 termination；最终终止必须持有已核对 creation/executable 的自有 HANDLE。

同 requestId 在不同 owner 下合法存在，活跃 lease 匹配必须同时绑定 owner/request/generation 或原 operation key，不能使用 requestId 全局集合替死请求保活。以上为静态修复条件；未调用旧逻辑或执行任何应用/测试。


## 实现收尾状态更新（2026-10-12，未执行检查）

静态读取确认公开正文准备已经进入既有 request-admission、parse application 与 operation 查询合同，前端接线和真实 service 测试代码仍由对应 owner 完成。RAG controller 已将 step intent/result 日志放到每一步执行周围，以保证持久化失败发生后下一步尚未执行；相应用例仍待最终候选检查。auth POST/PUT/PATCH/DELETE 现与 Research writer 使用同一 admission drain，活跃 lease 使用完整 operation key 回收，防止不同 owner 同 requestId 互相保活。Windows process liveness 现读取 creation identity 与状态，不用 os.kill 查询 Windows 进程；静态 primary-source 依据另存 windows-process-liveness-static-reference-20261012.json。以上是代码阅读记录，不是测试或实机成功证据。


## 最终实现静态核销（2026-10-13）

backend/frontend/runtime 三方最终 handoff 的 confirmed production/test 缺口均为空。主控核对了 auth writer drain/operator/MCP fail-closed、prepare exact durable receipt、owner PDF/source hash、RAG step journal 与 abstention gate、typed exact-run public report、before-send durable identity、same-page owner response isolation、canonical route/CAS migration、notebook/selection/续读、bounded owned lifecycle/backup/restore/rollback。第 1–10 章代码任务均可核销；各任务 source/test 的完整映射见三个 owner handoff 与 implementation-coverage-20261012.json。

root 对 backend/research imports 的静态扫描未发现 interfaces/infrastructure/legacy paper_radar 依赖。候选源码、解释器与依赖以 candidate-freeze-before-validation-20261013.json 为初始冻结；后续根因修复必须记录新候选并重跑所需最终检查。Windows ACL/快捷方式/registry、真实 parser/provider/PDF、实际浏览器缩放与电脑重启等仍属于第 12 章，没有运行或通过宣称。

## 集中验证根因修复复核（2026-10-13）

首轮回归修复包含显式 actor_scope 的 transcript/replay 调用、typed corrupt-journal 处理、reader answer 的 canonical evidenceId 与 run/paper/source metadata，以及符合当前真实 use-case 合同的 test fixture。没有添加不带 gate 的 production RAG fallback；PermissionError 与 scope mismatch 仍拒绝访问。多进程测试改用仓库 cwd 下的模块入口，保持实际跨进程锁与去重断言。

第二轮出现的两个问题来自测试资源生命周期。并发测试将 schema/storage 构造移到线程启动前，八方 barrier、15 秒 timeout 和幂等断言不变。备份 fixture 显式 commit/close SQLite connection，新增 GC 后数据库字节稳定检查；WAL snapshot 用例显式持有连接并验证主库与 WAL 中两条记录。production recovery 的 writer guard 未改。两份最终测试文件已逐行核对。

第三轮 Python 候选见 backend-release-candidate-identity-20261013.json：与第二轮仅上述两份测试文件不同，源码 2,770 文件、87 项 Python distributions。前端独立冻结身份见 frontend-release-candidate-identity-20261013.json；未重新构建或变更已验证的 frontend。全量与 mandatory smoke 的实际结果仍由执行记录单独绑定，本文不声明第 12 章实机验收。

## 最终自动关卡收尾（2026-10-13）

最终 compile、完整 scripts.dev test 和必需 scripts.dev smoke 均 exit 0。全量 9,187 passed / 117 skipped / 24 deselected；smoke 的 Harness/Research/API/service/recorded composition/architecture pytest 为 4,192 passed / 23 deselected，随后 AgentLoop offline 成功且 network_calls=0，sources validation valid、0 errors、0 warnings。实际跳过原因与既定 live_research_e2e 排除范围保留。

检查后重新读取 2,770 个 Python 源码文件、87 项 distributions、项目和基础 Python executable，以及 813 个 frontend 文件、1,004 个生产 build 文件和 Node executable；全部与冻结记录一致。前端最终单元测试 729 项、浏览器独立用例 75 passed / 6 既有 Studio 跳过、lint/typecheck/build 均通过，递归 Node tree 无问题。17 项既有依赖审计告警保留，不声明全量漏洞扫描为零。

第 11 章据此核销；第 12 章真实账号、provider、PDF、资料备份、隔离恢复、实际 release 启动及电脑重启验收尚未执行，其他 active change 未修改，也不按本轮自动 fixture 证据归档或核销。
