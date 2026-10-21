# 集中验证证据索引

原候选 `72fbd1d` 的第 1–10 章实现和第 11 章集中验证已完成：前端 729 项单元测试、75 项浏览器用例、typecheck/lint/local-desktop build 通过；后端 9187 passed / 117 skipped / 24 deselected，必需 smoke 4192 passed / 23 deselected，AgentLoop offline 与 sources validation 通过。以下原有 “final” 记录属于该候选。第 12 章的真实执行已发现新问题并形成新源码，旧结果不证明新修复通过；本轮重新检查与真实验收以新增证据及其 source/release identity 为准。

- [frontend-release-candidate-validation-20261013.json](frontend-release-candidate-validation-20261013.json)：最终 clean-install、729 项单元测试、typecheck、lint、生产 build、75 项浏览器用例、6 项既有 Studio 跳过、递归依赖树及残余审计告警。
- [frontend-release-candidate-identity-20261013.json](frontend-release-candidate-identity-20261013.json)：最终前端源码和生产构建的逐文件身份；production build 未激活为本机 release。
- [final-automated-validation-20261013.json](final-automated-validation-20261013.json)：本轮集中验证的总记录、实际跳过范围、静态审查和第 12 章待验收边界。
- [spec-validation-final-20261013.json](spec-validation-final-20261013.json)：最终 OpenSpec strict 校验实际返回 valid。
- [final-validation-integration-audit-20261013.json](final-validation-integration-audit-20261013.json)：提交范围、秘密特征、JSON/日志校验和、文档链接及最终 diff 检查。
- [backend-release-candidate-mandatory-smoke-20261013.json](backend-release-candidate-mandatory-smoke-20261013.json)：必需 pre-commit gate 的 compile、pytest、AgentLoop offline 与 sources validation 完整退出证据。
- [backend-release-candidate-full-tests-20261013.json](backend-release-candidate-full-tests-20261013.json)：最终完整后端套件的实际退出结果、117 项跳过原因与冻结源码身份。
- [backend-release-candidate-identity-20261013.json](backend-release-candidate-identity-20261013.json)：最后两处 fixture 修复后的 Python 源码与解释器/依赖身份；最终全量与必需 smoke 使用该冻结候选，检查后逐文件与解释器/依赖重新核对。
- [backend-repaired-candidate-final-validation-20261013.json](backend-repaired-candidate-final-validation-20261013.json)：第二轮失败回归的历史源码身份，不作为最终通过证据。
- [backend-18-failure-repairs-targeted-validation-20261013.json](backend-18-failure-repairs-targeted-validation-20261013.json)：18 项已观察失败对应的 89 项回归通过记录。
- [runtime-recovery-final-contracts-20261013.json](runtime-recovery-final-contracts-20261013.json)：显式关闭 SQLite fixture 连接后，完整 backup/recovery 模块 29 passed；WAL snapshot 与 GC 稳定性断言保留。
- [runtime-all-contracts-after-final-audit-repairs-20261013.json](runtime-all-contracts-after-final-audit-repairs-20261013.json)：Windows runtime/恢复合同 138 passed；2 项 symlink 权限环境跳过保留原始理由。
- [backend-full-validation-failed-attempt-20261013.json](backend-full-validation-failed-attempt-20261013.json)：首个完整后端回归的 18 failed / 9167 passed / 117 skipped / 24 deselected，仅为失败历史，不能作为最终通过证据。
- [backend-full-validation-second-failed-attempt-20261013.json](backend-full-validation-second-failed-attempt-20261013.json)：第二轮完整回归的两项失败、117 项实际跳过理由及冻结源码绑定；仍为失败记录。
- [validation-queue-20261012.json](validation-queue-20261012.json) 与 [validation-root-cause-repairs-20261013.json](validation-root-cause-repairs-20261013.json)：执行历史、实际根因修复和当前验证状态。

早期 `candidate-freeze-before-validation`、`frontend-final-validation`、首次浏览器诊断和 Next 14 检查属于历史记录，后续修复已产生新的源码与检查身份。应按上述最终候选记录及其状态判断结果，不将早期同名 “final” 文件视为最新结果。

日志以 `.log.gz` 保存精确原始字节，gzip 的时间字段固定为 0。归档字节 SHA-256 和解压后的原始日志 SHA-256 见 [validation-log-archives-20261013.json](validation-log-archives-20261013.json) 及各记录的 `logArchiveIdentity` / `decompressedSha256`；运行时元数据中的原始 TEMP 路径和原始日志校验和保留。没有修改日志文本、测试选择或断言来通过检查。

浏览器 route fixture、fake worker/repository 与自动合同测试仅证明对应的自动验证行为。真实账号、provider、PDF、资料备份与隔离恢复、实际 release 启动、预算和用户电脑重启证据按第 12 章分别核销。

第 12 章真实执行证据（2026-10-13，范围与限制逐项注明）：

- [live-preflight-20261013.json](live-preflight-20261013.json)：现有真实配置、缺失能力与旧候选边界。
- [live-acceptance-plan-20261013.json](live-acceptance-plan-20261013.json)：有界计划、初始化前有效保护快照、费用与恢复限制。
- [live-runtime-attempts-20261013.json](live-runtime-attempts-20261013.json)：实际构建/启动失败与修复后的真实成功，失败尝试保留。
- [live-production-start-20261013.json](live-production-start-20261013.json)：production build、API/BFF/raw/runtime identity、loopback、匿名私有 API 拒绝、重复 start、有界日志与桌面入口；不宣称整个业务闭环通过。
- [actual-port-conflict-20261013.json](actual-port-conflict-20261013.json)：真实非所属监听器被保留，启动拒绝端口冲突。
- [existing-catalog-isolation-20261013.json](existing-catalog-isolation-20261013.json)：原 catalog 不可读时，已复制真实 catalog 的 63 个模型、24 个 artifact 与 6 个 chunk manifest 的隔离验证；不是正式 backup/restore 或用户重新登录证据。
- [live-login-ui-20261013.json](live-login-ui-20261013.json)：真实未登录初始化页的桌面视口与键盘检查；125% 采用有效 CSS 视口，登录后的完整界面与实际 OS 缩放仍待验收。
- [live-legacy-routes-20261013.json](live-legacy-routes-20261013.json)：旧路径真实 HTTP 迁移、query 保留与未登录正式 reader 的认证跳转；不替代真实来源/续读验收。

本轮所有原始运行输出先保留在受控 `.tmp/desktop-acceptance/20261013`，这里只提交脱敏摘要与 checksum；不提交 `.env`、账号/口令、cookie、session、真实用户内容或完整业务备份。正式 backup、隔离恢复后重新登录、模型分析与报告、最终构建核销、电脑重启尚未完成时，相应第 12 章任务必须保留未勾选。

- [live-pdf-parser-20261013.json](live-pdf-parser-20261013.json)：不可变生产版本真实解析器通过公开 PDF 的 15 页确定性文本质量检查；尚未执行认证导入、发布或 reader/笔记/报告流程。
- [live-route-cache-failure-20261013.json](live-route-cache-failure-20261013.json)：真实页面请求后 38 个 Next.js 路由缓存文件与 1 个 API audit 文件导致严格资源校验失败；该旧发布不得用删缓存或放宽清单的方式重新声明通过。

- [live-provider-preflight-20261013.json](live-provider-preflight-20261013.json)：真实配置与真实 Research candidate schema 的只读预检；native projection 被确定性拒绝，零网络请求、零付费模型调用，未通过 12.6。

- [live-stop-failure-20261013.json](live-stop-failure-20261013.json)：v3 真实停止失败，已退出的日志进程被误判，API 尚未清理且 STOPPING 丢失 boot identity；保留失败记录，修复后须重新做实际生命周期验收。

- [live-legacy-stop-repair-20261013.json](live-legacy-stop-repair-20261013.json)：修复后真实停止旧 v3，12 个原 PID 与 3000/8000 listener 全部消失；未修补旧 boot identity，最终候选仍须验证正常/异常停止和重启。

- [live-audit-path-failure-20261013.json](live-audit-path-failure-20261013.json)：v6 cache 和 pyc 均为零，但真实 API audit 写入 immutable app，strict 校验明确拒绝；保留精确 inventory diff 和失败版本，修复默认写入路径后须重新 build。

- [live-standard-backup-precondition-20261013.json](live-standard-backup-precondition-20261013.json)：真实标准备份因 required auth owner 尚未初始化而拒绝，未创建 ZIP；不制造空业务存储补齐验收，保护快照不当作标准备份。

- [live-rag-composition-failure-20261013.json](live-rag-composition-failure-20261013.json)：真实 configured composition/lazy provider 重现必填 transcript root 漏传的 TypeError；v7 build 通过但未启动，保留失败与待修复边界。

- [live-final-runtime-acceptance-20261013.json](live-final-runtime-acceptance-20261013.json)：最终 v8 实际 Windows 启动、API/BFF/loopback、幂等 start、正常与异常停止、资源清理、正式 restart、日志轮换与运行后的两次完整发布清单校验；核销 12.2/12.8，真实账号、PDF 导入、模型和正式备份恢复仍保留未完成。

- [live-final-validation-incomplete-20261013.json](live-final-validation-incomplete-20261013.json)：v8 首次最终 full 出现 F 后被执行者提前中止，未取得 traceback/自然退出/最终汇总；并行 smoke 也中止。两个日志只作不完整历史，不能算最终门禁通过。

- [live-final-frontend-binding-20261013.json](live-final-frontend-binding-20261013.json)：前端 731 项 unit、typecheck/lint/build、75 项 fixture E2E 与最终 v8 的逐文件身份核对；4 项桌面 fixture 在最终 production server 复验通过，未作为真实业务成功。

- [live-final-root-cause-fixes-20261013.json](live-final-root-cause-fixes-20261013.json)：owned 写入路径、catalog production provenance 和 configured RAG transcript root 的实际根因及自动检查边界。

- [live-final-provider-preflight-20261013.json](live-final-provider-preflight-20261013.json)：以最终 v8 配置和真实 candidate schema 再次核对 provider；native projection 继续拒绝，零 HTTP、零付费调用，recorded reference release 不能作为外部部署资格。

- [live-final-secret-boundary-audit-20261013.json](live-final-secret-boundary-audit-20261013.json)：提交范围与最终前端静态产物的已知秘密匹配审计为零；根 `.env` checksum 未变。两项字符串特征命中已核对为离线测试凭据，未读取用户口令或 cookie。

- [live-final-account-precondition-20261013.json](live-final-account-precondition-20261013.json)：最终运行版本仍未初始化真实账号；匿名 session 返回 200/initialized=false，私有 reports 返回 401。12.3 继续未完成，未造账号、未读取口令或 cookie。

- [live-catalog-canonical-path-failure-20261013.json](live-catalog-canonical-path-failure-20261013.json)：隔离 project `.venv` 诊断确认旧已提交绝对 manifest 路径先被规范化，dot/parent/重复分隔符在 reader 与恢复 provenance 三类入口共 9 次被接受；待补原始字符串拒绝规则，不算最终交付通过。

- [live-v8-natural-smoke-20261013.json](live-v8-natural-smoke-20261013.json)：主控自然运行的 v8 smoke 通过（4192 passed），与 2624 个冻结 Python 文件一致；最终交付仍须路径修复后的新候选全量验证。

- [live-v8-natural-full-failure-20261013.json](live-v8-natural-full-failure-20261013.json)：主控自然全量结束，1 failed /9239 passed /117 skipped；保留具体失败节点及 traceback 日志 checksum，须修复并重新验证最终候选。

- [live-catalog-canonical-path-repair-20261013.json](live-catalog-canonical-path-repair-20261013.json)：原始绝对路径的 reader/provenance 校验修复及 86 项 focused 回归通过；最终替代发布与其全量门禁另行绑定。

- [live-timeout-regression-repair-20261013.json](live-timeout-regression-repair-20261013.json)：修复 10ms OS 调度假设的超时回归，保留生产截止/取消语义和全部原断言；50 项相关检查与 1000 次受控回归通过，最终全量/smoke另行绑定。

- [live-v9-candidate-20261013.json](live-v9-candidate-20261013.json)：实际替代发布 `20261013T142952Z-1006169e2757`、42,383 个资源与2624个Python文件冻结身份；最终自然full/smoke和生产验收按后续结果更新。

- [live-v9-packaged-catalog-canonical-20261013.json](live-v9-packaged-catalog-canonical-20261013.json)：真实 v9 打包代码的 9 次原始绝对路径拒绝与 artifact/marker 不变、无 provenance 生成；隔离合成负例，不替代真实资料恢复。

- [live-v9-provider-preflight-20261013.json](live-v9-provider-preflight-20261013.json)：实际配置的 provider 在打包候选中仍被 schema 投影拒绝，付费调用为零；保留辅助工具初次定位失败，不计为真实模型验收。
- [live-v9-public-pdf-parser-20261013.json](live-v9-public-pdf-parser-20261013.json)：实际公开 PDF 经 v9 parser 解析为15页、44,609字符并通过text admission；尚未完成认证导入/发布reader。

- [live-v9-runtime-acceptance-20261013.json](live-v9-runtime-acceptance-20261013.json)：最终替代候选的真实Windows启动、重复启动、正常停止、端口冲突、日志轮换、资源严格复验与异常退出/恢复进度；只核销已完成实机范围。
- [live-v9-standard-backup-preconditions-20261013.json](live-v9-standard-backup-preconditions-20261013.json)：运行与停止状态的真实标准备份拒绝，required owner尚未初始化且无归档；真实备份恢复保持待办。
- [live-v9-frontend-binding-20261013.json](live-v9-frontend-binding-20261013.json)：14 个已检查前端文件与当前/最终包一致，绑定731单元、75fixture历史自动结果和当前v9实际4项桌面fixture。

- [live-v9-secret-boundary-audit-20261013.json](live-v9-secret-boundary-audit-20261013.json)：最终发布静态资源与371个范围文件的7个已知秘密值命中0；桌面入口已安装、自启关闭，未读取用户口令或cookie。

- [live-v9-account-precondition-20261013.json](live-v9-account-precondition-20261013.json)：最终restart后账号仍未初始化，匿名session200、私有直连/BFF401，真实账号验收未关闭。


## v9 输入隔离与真实用户入口

共享工作区全量/smoke 在运行期间出现另一项 RAG 开发的源码和测试改写。两套命令继续自然退出并保留原始记录，但不得授予最终 v9 门禁通过；最终检查改用源码摘要及全部冻结 Python bytes 与实际 v9 相同的独立 worktree。该范围限制不撤销已经完成的真实生产包生命周期与资源完整性证据。

- [live-v9-parallel-workspace-scope-20261013.json](live-v9-parallel-workspace-scope-20261013.json)：并行新增文件、冻结输入漂移时间与原始 SHA、共享门禁范围限制和隔离处理。
- [live-v9-isolated-candidate-binding-20261013.json](live-v9-isolated-candidate-binding-20261013.json)：2,624 个准确冻结 Python 文件、2,512 个发布源码输入及完整 source_digest 与 v9 的绑定；前后核对全部 inputs 的最终捕获器已准备。
- [live-v9-dependency-snapshot-reconciliation-20261013.json](live-v9-dependency-snapshot-reconciliation-20261013.json)：v8 的 87 条与 v9 的 85 条快照差异归因；两份源树 egg-info 和 17 个文本换行规范化差异已逐个核对，实际 venv 依赖未被改装。
- [live-v9-user-browser-entry-20261013.json](live-v9-user-browser-entry-20261013.json)：实际 production 登录页已在用户可见浏览器打开；账号由用户自行选择，未读写密码或 cookie 值，尚不核销 12.3。
- [live-v9-shared-workspace-smoke-failure-20261013.json](live-v9-shared-workspace-smoke-failure-20261013.json)：共享工作区 smoke 自然退出，3 failed、4,189 passed；并行新增 RAG 文件触发架构边界失败，原始日志和范围完整保留，不授予最终 v9 门禁通过。
- [live-v9-shared-workspace-full-failure-20261013.json](live-v9-shared-workspace-full-failure-20261013.json)：共享全量自然退出，1 failed、9,245 passed、117 skipped；外部写入超时测试的 calls==1 断言实际为0，失败测试 bytes 与 v9 冻结输入相同，单独核对根因，不授最终门禁通过。
- [live-tool-timeout-test-repair-20261013.json](live-tool-timeout-test-repair-20261013.json)：外部写/MCP 两条 timeout 测试的执行入口前提修复；私有完整文件 7 passed、两用例各30次通过，生产模块与实际包逐字节一致。补丁已进入 `.tmp/dvf` 的最终 test revision，原冻输入与失败事实保留；新 full/smoke 等待自然结束，不关闭业务验收。
- [live-v9-isolated-smoke-20261013.json](live-v9-isolated-smoke-20261013.json)：准确 v9 原冻结输入的 smoke 自然通过，4,192 passed；compile、离线 AgentLoop、source validation及完整前后输入/85依赖身份均通过，不替代待集成测试修复后的最终门禁。
- [live-v9-isolated-full-20261013.json](live-v9-isolated-full-20261013.json)：准确 v9 原冻结 Python 输入的 full 自然失败，1 failed、9,245 passed、117 skipped；缺少 checkout 中未跟踪的必要政策文件，完整 traceback 和前后 Python/85 依赖身份检查保留，不能授予最终门禁通过。
- [live-v9-isolated-auxiliary-input-repair-20261013.json](live-v9-isolated-auxiliary-input-repair-20261013.json)：下一轮隔离输入补齐历史原字节的 pending authority 政策，生产代码未修改、权限未激活；计划加入非 Python 输入前后绑定并复验最终 full/smoke。
- [live-v9-tool-timeout-testfix1-prestart-20261013.json](live-v9-tool-timeout-testfix1-prestart-20261013.json)：真实最终 test revision 准备检查通过，单份 Tool 测试变更、2,624 个 Python 输入、85 依赖和原始 pending policy 均已绑定；最终 full/smoke 已执行，尚未声明通过。
- [live-v9-tool-timeout-testfix1-smoke-20261013.json](live-v9-tool-timeout-testfix1-smoke-20261013.json)：最终完整输入测试修订的 smoke 自然通过，4,192 passed；compile、离线 AgentLoop、source validation、2,624 个 Python 输入、85 依赖和 pending policy 前后绑定均通过。full 仍在自然运行，不关闭真实业务验收。

## 2026-10-14 续接与最终门禁恢复

- 续接原 chat 的第 12 章真实本机目标，正式记录为 `live-v9-continuation-20261014.json`；任务维持 112/124、第 12 章 2/14，不授予完整交付资格。
- 旧 full 会话 `76448` 已不可读取，系统无对应进程且无最终 metadata，原日志约 98%；保留为未完成，不能据没有 `F` 标记推导通过。testfix1 smoke 的自然通过及前后输入绑定保留，但不替代 full。
- 同一冻结输入恢复的完整 full 正在自然执行；长临时根触发已知快照持久化失败。相同测试源码的短/长路径对照为 2 passed / 2 failed，私有因果观察保留原调用并原样 re-raise，确认 Windows rename 目标长度 263。已准备与原有效执行条件同长度（36）的独立短目录，完整选例和默认排除不变；只在旧运行自然终止且绑定通过后受控启动一次，不取消旧运行或覆盖失败。
- 真实生产浏览器记录覆盖未初始化登录页的 1366×768、1920×1080、完整 Tab 到用户名/口令/提交按钮以及正式/旧入口转向，无 route mock、账号写入或口令/cookie 导出；这些不是认证工作台、真实 PDF/报告、OS 125% 或电脑重启验收。
- 已打开用户默认浏览器并请求用户在本机自行初始化。provider 仍是 held/disabled、native mode none，定价与显式费用预算尚未就绪；未绕过 gate、未发起付费调用。真实标准恢复及最终提交仍须完成对应真实前提和门禁。

续接工具的初次独立入口失败已原样保留；修复了直接脚本 `sys.path` 与 Windows virtualenv launcher/实际 runner PID 的区别，15 项私有回归及非项目 cwd 入口检查通过。v2 隐藏 helper 已真实启动并确认 `waiting_for_original_natural_exit`，最多启动一次短路径完整 run，不信号旧进程、不在观察超时时盲目重启。真实匿名浏览器访问后再次严格验证全部 42,383 个发布资源，manifest 不变、bytecode/route-cache 文件为 0；已知根环境秘密检查 400 文件/7 值/0 命中。上述证据仍不替代全量自然结果或第 12 章真实认证/模型/恢复/电脑重启验收。

### 2026-10-14：两版本只读前置、真实 OS 基线与提交范围复核

- 旧 v8 `20261013T112201Z-3a4c3705242c` 的真实 release 已完成只读严格校验，自然 exit 0、42,383 个资源、配置与依赖身份通过、manifest 前后不变，pyc/pycache/route-cache 均为 0。v8/v9 manifest 声明的 15 个 owner readable/writable schemas 完全一致；这只证明声明和资源级前置，不证明当前真实数据兼容，也没有启动 v8、切换版本或执行不兼容拒绝。12.10/AC-14 保持未完成，详见 `live-v9-two-release-preflight-20261014.json`。
- 实际 Windows 只读基线：两台 2560×1440 显示器的 OS 缩放均为 100%，不是 125%；`LastBootUpTime` 为 `2026-10-08T08:49:48.5Z`，早于本次部署。未改 OS 设置或发出重启，认证 UI/部署后电脑重启证据仍缺失。12.11/12.12 保持未完成，详见 `live-v9-os-baseline-20261014.json`。
- 当前待提交 desktop 源码的 27 个 Python 文件匹配最终冻结输入，14 个 frontend 文件匹配既有 checked-source 与隔离副本，13 份既有 frontend 检查日志实际 SHA-256 一致。这是检查适用性复核，不是最终 full 已通过；git index 未修改、尚未提交。RAG evaluation protocol 的并行改动及新增 `pyproject.toml`/`requirements/` 变更不纳入 desktop 范围。
- 真实匿名账号复查按 BFF `data` envelope 合同提取，`2026-10-13T20:31:07.394422Z` 仍为 `initialized=false`、未认证；首次只看 top-level 的 null 结果明确归为无效解释并保留，不作为账号状态证明。旧 full “running” 历史快照已在 candidate 中保留并更正为原次未完成、当前恢复运行自然结果未定；不会把部分失败记录解释成最终通过。详见 `live-v9-continuation-20261014.json`。
- 补充路径前提核对：在与当前完整 full 相同长度的 85 字符、新建独立临时根上，两项既有失败用例再次自然失败（2 failed），测试源码摘要前后一致；原 35 字符对照为 2 passed。没有触碰正在运行的临时根、取消 full 或用选例替代完整门禁。
- 最终聚焦提交除 41 个 desktop 源码文件外，必须恢复一个既有必要辅助输入的跟踪：`openspec/changes/durable-event-runtime/evidence/compatibility-observation-policy.json`。当前主根/隔离根均为历史原始 1,515 字节，SHA-256 `9512d717fa910e1c5d00a757d473f61381def9e6c7353c83d3a1a464a3e303df`，但仍 ignored/untracked；最终须精确恢复这些字节，而不是改变测试或造新资格。其 `pending_external_activation`、null trust epoch/authorities 和原 owner 资格边界保持不变；未授予 durable-event qualification，git index 此轮仍未改。
- 逐项 AC 审计已落盘为 `live-v9-ac-evidence-audit-20261014.json`：32 份来源在整合时实际摘要一致，并在 PRIVATE 平坦输入快照保留原字节。17 项中仅 AC-12 在既有 Windows 生命周期范围内有完整直接证明；7 项 partial、9 项 missing，全项最终核销标记均为 false。本报告记录自动/真实/匿名/fixture 边界、缺口和下一观察，不代替最终 full 或第 12 章业务闭环；其 source hashes 是审计时点快照，后续状态更新不会被误当成旧输入仍实时不变。

### 2026-10-14：完整门禁自然终止与短路径实际接续

- 恢复的长临时根 full 在 `2026-10-13T20:56:05.883681Z` 自然 exit 1：37 failed、9201 passed、117 skipped、24 deselected、579 warnings、8 errors；全部 2,624 个 Python 和 85 个依赖及辅助输入的前后绑定一致且通过。完整失败保留，不因同源码短/长对照忽略额外失败或错误。默认 `-r s` 不打印 FAILED/ERROR 短摘要 node ID；记录使用实际 37 个失败/8 个 setup 错误标题和最终 summary，不把零正则匹配误报为零失败。详见 `live-v9-full-natural-handover-20261014.json`。
- 受控 helper 已在可信自然失败后启动且只启动一次短路径完整 full；父独立核实 launcher PID 55152 与实际 runner PID 65936 的 FILETIME/exe，以及 bootstrap receipt 的 actual PID/birth/parent/config/runner 摘要一致，未公布 nonce 值。绑定 runner 与冻结输入未改，仍是完整默认选例；当前自然结果尚未取得，不能提交本批待验证业务修复或授予最终通过。
- 默认非网络 Doctor 真实执行一次，自然 exit 0、status warning、stderr 为空。直接三态和时间已补齐：PDF 与 artifact storage 的 reachable=yes 但 verified=unknown；分析/问答的 configured=yes、reachable/verified=unknown；没有启用 `--probe-network` 或发起付费调用。CLI 仅直接输出 release ID，缺完整三元 digest，不补造字段；缺解释器/provider 等负例仍未由本次覆盖，AC-01 继续 partial。详见 `live-v9-doctor-three-state-20261014.json`。
- 本轮增量保密边界检查：406 个候选公开 payload/发布静态文件与84个 PRIVATE文本记录，对7个当前根环境已知敏感值均0命中；不输出值/上下文，不读用户口令或 session cookie。该结果是检查时点、已知值和声明扫描范围的结论，不对未知秘密或尚在增长的日志作全域保证。

### 2026-10-14：真实模型资格与费用前置复核

- 当前匿名 BFF 真实结果为 `initialized=false`，未创建账号或读取口令/cookie。当前模型仍是 `dashscope-deepseek-v4-flash`、native mode `none`，deployment pricing 为空。实际调用与 v9 发布字节一致的 provider release gate：held 记录因未批准/禁用/评测未通过被拒绝；approved 的 recorded reference 因 provider/deployment 和 capability revision 不匹配被拒绝，当前可用真实 release 为 0。
- 两份供应商公开文档已有限 GET 并保留原始 SHA-256；文档中的 JSON Schema 模型只能作为后续受控资格候选，不能据此授予本机资格。未安装数字定价、汇率或预算，未修改路由/资格，也未发起付费 generation。真实账号、预算批准、真实观察/held-out gate、shadow/promotion 和业务验证仍是 12.1/12.3/12.6 的前提。详见 `live-v9-model-prerequisites-20261014.json`。
- 当前同配置 credential 的模型目录仅做 1 次 HTTPS GET，200、176 entries：`qwen3.8-flash` 列于返回目录，配置中的 `deepseek-v4-flash` 未列于本次返回目录；未列出不等于已实测 generation 拒绝，列出也不等于已取得 generation/JSON Schema 资格。请求禁重定向、有 12 秒/1 MB 上界，没有调用 generation endpoint；原始 credential/header 未记录。路由、定价、费用批准、业务验证均未据此变更。
