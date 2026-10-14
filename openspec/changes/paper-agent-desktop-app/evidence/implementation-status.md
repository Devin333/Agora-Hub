# 实施状态与验收边界

日期：2026-10-14，Asia/Shanghai。用户已授权「按照这个文档开发」。本记录为实现阶段证据，不是发布资格声明。

## 工作区与所有权

- 本变更基线 commit：`dacdbbfd`（PRD 文档提交）。共享工作区仍有其他任务的 pending 源码与测试。
- 原始 status、HEAD 和集成文件逐字节基线保存在本地 `.tmp/paper-agent-desktop-app-20261014/`，不作为生产数据。
- 各 worker 使用不重叠写范围：personal-memory core；正式前端；desktop host；runtime adapter；memory API/route isolation；task-memory context。
- 原 3000/8000 与 3107 服务均仍存在。开发候选 App 使用独立 3120/8120，未经归属验证不得接管或停止原进程。

## 已实际检查

| 范围 | 证据 / 当前状态 |
| --- | --- |
| Personal memory domain/application/SQLite | 已新增核心文件；主控用项目 `.venv/Scripts/python.exe` 重跑定向测试：19 passed、1 skipped，2.53s |
| 核心状态与隐私 | candidate/active/disabled/deleted、CAS、owner namespace、local-only recall、精确 ID/revision 快照、默认关闭远程策略已有真实代码与测试 |
| Windows reparse 覆盖 | 原 symlink 测试因 WinError 1314 跳过；正在补充无需该权限的独立 junction 回归，不能把 skipped 项说成验证通过 |
| OpenSpec | 本提案原 strict 校验已通过；实现后每次有实质规格变化再次校验 |
| 全量 smoke | 尚未针对最终实现重新运行。历史共享工作区已有未登记 adapter contract 的架构失败；需集中处理与最终冻结输入校验 |
| 桌面安装、真实模型与完整 AC | 尚未通过；仍是待完成交付条件 |

核心测试日志：`.tmp/paper-agent-desktop-app-20261014/personal-memory-core-tests.log`。模型候选不等于确认记忆，SQLite 保存不等于云端授权。

## 发布验收矩阵

以下 AC 均保持未核销，必须以同一最终桌面构建逐项证明。

| AC | 证明位置 / 必需证据 | 当前结论 |
| --- | --- | --- |
| AC-01 | 安装构建、真实窗口、非开发环境入口与 single writer 记录 | 未验证 |
| AC-02 | 实际双主题 App 截图中仅 Agora 的侧栏品牌 | 实现中 |
| AC-03 | 空用户库、真实身份/owner 与负向授权测试 | 实现中 |
| AC-04 | 真实工具来源、零结果/失败区分、本地筛选无外发 | 实现中 |
| AC-05 | paper/source/digest 身份、真实 PDF、重开离线原件 | 未验证 |
| AC-06 | 全文/摘要/部分解析与定位/续读 | 实现中 |
| AC-07 | 合格 provider 的真实回答、request/run/gate、原文引用定位 | 未验证 |
| AC-08 | 真实笔记/报告/导出、冲突与写入失败回读 | 实现中 |
| AC-09 | 核心已有单测；还需 App 用户确认和重开回读 | 部分证据，不算通过 |
| AC-10 | 核心已有单测；还需管理 API/UI、实际重启与索引恢复 | 部分证据，不算通过 |
| AC-11 | 核心 policy/snapshot 已有；还需实际上下文与网络断言 | 部分证据，不算通过 |
| AC-12 | 实际 App 断网后的阅读/笔记/记忆操作 | 未验证 |
| AC-13 | 真实 request 身份、超时/退出恢复、无重复调用 | 未验证 |
| AC-14 | 新记忆 owner 的备份 inventory/schema 与隔离恢复记录 | 未验证 |
| AC-15 | 实际分辨率/缩放/黑白主题与键盘输入法检查 | 未验证 |
| AC-16 | 宿主/接口/来源安全、no-fixture 依赖边界、全部 gate | 未验证 |

## 尚需确认的真实验收输入

真实 provider/来源及调用/费用上限要在执行付费验收前确认；当前代码开发与本地无网络测试可继续。未获许可不会自行启动付费模型调用。当前未将 App 重开等同于用户电脑重启，也不会擅自操作系统重启。

## 集成复核补充（同日）

- 主控已用真实项目解释器复跑 memory domain/application/SQLite + API + composition 五组测试：`30 passed, 1 skipped`，8.97s；跳过的仍为需 Windows symlink 特权的用例，不能核销 junction 安全条件。
- 新增运行依赖复核记录见 `runtime-dependency-inventory.md`；managed Python 的关键 native imports 成功，Node 官方 zip checksum 已核对。两者仍是构建输入，不是已验收安装包。
- 路径审阅发现 composition 与 runtime adapter 在拒绝链接前调用 resolve 的风险，已要求在原输入链上验证并增加真实 junction 负向回归；修复和回归完成前不核销安全项。
- 正式 UI 保存状态必须由真实 workspace receipt/语义回读确认，而非乐观数组插入或单独增加的时间戳；记忆披露必须与实际问答/分析发送的 frozen refs 一致。
- 原文阅读、native 导出、退出 drain、memory inventory 与隔离恢复仍在集成，尚无安装/付费模型验收通过结论。

### 共享集成检查更新

- 主控已集中复跑 desktop adapter/bundle/protocol/config/runtime、personal-memory recovery、memory API/composition：`56 passed`，10.91s。覆盖默认 CRUD 与真实 desktop core 共用实例、并发单次构造、custom factory 不创建第二 store，以及 Windows junction 拒绝分支。
- memory inventory 复查只出现一个 `research_personal_memory` owner，真实 schema/path/SQLite consistency；已经没有 `pending_inventory_extensions` 占位字段。
- 共享架构精确契约登记经根因复核后：`5 passed`。个人记忆的登记属于本变更；现有 evaluation adapter 登记为并行变更的共享依赖，最终 staging 不混入其生产改动。
- 当前源码 `python -m scripts.dev compile` 与本 change strict 校验通过。最终完整 smoke 尚未针对冻结候选运行，不能由定向结果替代。
- F-08 的数据位置、能力状态和诊断面板不是模型/检索配置编辑能力；安全配置入口尚需补齐或明确复用 owner。此条件不核销，不能把只读设置面板当作全部设置完成。

### 最近无收费检查与候选构建边界

- 个人记忆真实 junction 与任务上下文回归已有结果；完整架构检查的 legacy 反射误报经 AST 根因修复后为 `256 passed`。修复保留真实 legacy import / 反射 / 定义等负向约束，不改变业务载荷和质量断言。
- Electron `44.7.1` 已取得并校验官方 Windows binary，宿主真实故障/待配置窗口 E2E 通过；这不证明 `/paper-agent` 业务服务、安装入口或论文闭环已经就绪。
- 正式页面 fixture E2E 与 Reader 双主题单色回归通过，但主控截图复核仍发现 125% 等效缩放下阅读区过窄，正在修复导航/列表的响应式折叠及窄屏按次记忆控制。
- 正在通过唯一运行管理器构建首个中间 source release，日志位于私有 `.tmp/paper-agent-desktop-app-20261014/interim-source-build.log`。该构建仅用于提前暴露真实生产 build / 首启问题，不是最终冻结候选，不核销发布 AC。
- 已取得官方公开 PDF 的隔离验收输入并验证其为可提取文本的真实 PDF；未放入用户资料库，未打包样本，不据此宣称已完成 App 导入或问答。
- 在线检索的 guided intent 同样会使用模型；用户未确认 provider 与调用预算前，搜索、分析、问答均不执行真实付费验收。

### Release 路径防护的真实根因取证

主控在本任务私有临时目录建立指向同一 owned root 内部 target 的真实 Windows junction。Python 实测 `is_symlink=False`、`is_junction=True`、`stat()` 的 reparse 位为 false、`lstat()` 的 reparse 位为 true；旧 `reject_reparse_path` 对 junction 本身及其子文件均未拒绝。这不是跳过用例或理论风险，已委派运行管理 owner 做原始 lexical 链 lstat-before-resolve 的根因修复及无需 symlink 特权的真实 junction 回归。中间构建不得用于安全发布验收。

### 中间生产首启与认证阻塞

- 中间 release 经唯一 owner 在独立验收数据根启动，3120 / 8120 的 API、frontend、BFF、raw rewrite 与 runtime identity 健康检查均为 true；总体状态 `DEGRADED`，没有将 transport ready 当作远程能力合格。
- `/paper-agent` 未授权时真实回跳 `/login?next=%2Fpaper-agent`，首个账号页不含旧新闻/项目导航；页面不是 fixture。
- 实际首启提交暴露 BFF 的认证写转发缺少可信 Origin：BFF bootstrap 返回 `503 auth_service_unavailable`；同一隔离 API 的无 Origin 请求返回 `403 desktop_origin_forbidden`，携带 canonical Origin 的受允许 bootstrap 返回 200。已定位至 BFF `safeApiPost` 未给本机 auth 入站校验提供可信服务器 Origin，交接口 owner 做根因修复及负向回归，保持 API 边界不放宽。
- 直接 API 调用仅用于确认根因，在隔离根创建测试账号；不手工注入 token/cookie 绕过 BFF 继续用户流程，不据此核销首启、资料库或 App 业务闭环。实际 PDF 导入和记忆 UI 回读仍待修复后的构建。

### 响应式根因修复复核

正式壳层在 `<1200 CSS px` 时将导航/资料列表折叠为抽屉，窄屏助手保留按次记忆控制；typecheck、定向 lint、36 项 feature 单测与 14 项 fixture E2E 通过。主控已查看最新 1366 / 125% 等效截图，正文和来源/专注/内容 tabs 不再被四栏挤成逐字换行。已更新 `sidebar-1366-125pct-fixture.png` 并保存两个 `reader-*-125pct-fixture.png`。这些明确是等效 CSS viewport / deviceScaleFactor 的 fixture 证据，不冒充 Windows 原生系统缩放、真实论文或最终 App 安装截图。

### 首启与 portable 输入后续复核

- 首启认证的 BFF owner 已完成可信 server-origin 转发根因修复：bootstrap/login/logout 经现有同源校验后注入 server-owned canonical Origin，default profile 与 GET session 保持原合同。定向 auth `26 passed`、lint 与 typecheck 通过；实际下一构建回归尚未完成。
- 原生宿主当前 150s start 截止会早于中间 release 的完整资源校验结束。有限 10min interim envelope 只用于下一轮真实 profile 取证，保留 timeout / termination unconfirmed 的 UNKNOWN；不是最终发布性能条件。优化路径校验不允许跳过 checksum 或接受不可信缓存。
- portable staging 已实际尝试并失败。安全 trace 定位为 managed Python 初始 `shutil.copytree`，input-only 复现记录 4 个 WinError 3：Torch distribution 的 license 深层路径超过 Windows 普通路径长度。磁盘足够；不能删除许可证、漏掉资源或调整系统策略掩盖。正在补充仅内部 IO 的 Windows extended-length 路径支持与长路径 / junction 负向回归；失败阶段没有生成可交付 bundle。
- 必需 `python -m scripts.dev smoke` 的全范围集成轮正在运行，尚无通过结论；源在继续修复，此轮不是冻结候选的最终 gate。
- 唯一 owner 已安全 drain 并停止中间验收服务，返回真实 `STOPPED`；未停止 3107 开发预览或其他服务。

### 配置 owner 集成审阅要求

新论文配置不得覆盖既有 lifecycle `config.json` 或复用其 schema。应独立保存于 `desktop/config/research-runtime.json`，使用真实 optional inventory / schema 与派生 models 管理，保持原生命周期配置和本机恢复合同。主控已要求 apply 在安全 stop 前进行无副作用 admission，并以跨进程锁保护 stop 后再校验、CAS、单次 ticket、journal、不可变 release 绑定和失败恢复；旧票据或冲突不得先停止服务。以上仍需真实 resolve / 重开 / 备份恢复回归，不以 fake release owner 的成功单测替代集成证明。

### 本轮冻结交接与明确未完成项

- 运行适配 / 配置 owner checkpoint：生产文件编译通过，定向 `110 passed in 18.72s`，diff check 通过；这不是完整 smoke 或真实 portable stage。
- 认证 + 设置只读 / 维护 UI checkpoint：4 文件 `34 passed`，lint 和 typecheck 通过。模型/检索配置的 native typed bridge 和 renderer 编辑闭环尚未接通，不将只读面板说成 F-08 全部完成。
- 宿主 / credential worker 在交接时发生运行时 compaction 错误。现有 vault 文件和 tests 必须重新审阅，尤其单一固定 ref/env 与 namespace 合同；没有该 worker 最终验证结论，不按完成处理。
- 配置实现仍有已识别的必须修复项：crash 后 config resolve 早于 journal recovery、恢复后 environment / doctor / lifecycle 未重新投影、packaged reopen 可能重新激活初始 bundle 覆盖用户配置、restore 后尚未完成派生 models 与不可变 release 安全重绑定。
- Windows 长路径修复尚待真实 managed Python / Node staging 复跑；42k 资源校验性能及 ReleaseManager 全部同规模长路径覆盖仍需实测。10min interim 宿主 envelope 不是已验收发布性能。
- 全量集成 smoke 尚在运行，当前没有最终通过或失败报告；本轮无代码提交。文档与明确标记的 fixture 视觉证据已独立提交，不混入其他变更的生产代码或用户数据。
- 保持 `tasks.md` 未核销状态；已保存 PRD/规格合同不等于可安装 App 交付，更不等于 AC-01–AC-16 已通过。

### 本轮续开发：长路径与安全校验的实测

- 主控复跑 Windows common / bundle 回归：`23 passed in 3.44s`。真实 junction（包括 owned root 内部别名、root 和祖先）拒绝，长路径资源保留；未添加跨调用缓存或删除 checksum gate。
- 逐级路径检查移除重复 `lexists` 和已证明无链接链上的重复 resolve，仅直接 `lstat`，其他检查错误仍 fail-closed。受控 400 / 1000 文件中位耗时下降 59.1% / 57.6%。
- 主控通过唯一 ReleaseManager 对原 42,539 个资源进行完整校验：真实耗时 `74.25s`，不是通过 fixture 或跳过资源得到。
- managed Python 原始输入经真实长路径复制、完整源/目标 count/hash 对账成功，耗时 `74.39s`，仅排除已登记的额外 pip distribution 元数据。移位后 native/import probe 成功：CPython `3.12.14`、87 distributions、SQLite `3.53.1`、OpenSSL `3.5.8`。这是输入能力证明，不是安装包或 portable release 通过。
- 原中间 release 再次 staging 被新增配置 owner schema 严格拒绝，错误 `desktop_app_release_schema_incompatible`。不篡改旧 manifest 或放宽 schema；待冻结新源码构建后再真实 stage。
- 配置 crash/reopen/recovery owner、native 单一 credential/typed bridge、设置编辑由三个 disjoint workers 继续实现。全范围 smoke 与前端集成测试正在运行，当前没有最终 gate 通过结论。

### 本轮续开发：全前端与架构复核

- 主控复跑全前端单元测试：`161 files / 873 tests passed`，耗时 `16.42s`；随后 `tsc --noEmit` 通过。这是当前源码检查点，不等同于 native 配置 apply、实际宿主或安装包验收。
- 主控再次复跑完整 `tests/architecture`：`256 passed, 8 warnings in 168.85s`；不据此核销 App 全部安全和发布条件。
- 设置表单的重复文本断言已按实际“系统凭据”区域收敛，保留所有“已配置但未验证”提示。取消、冲突和缺失 authoritative configuration 均不能假成功清草稿；native apply 仍需 owner 协议与真实验证后才能交付。
- 运行配置 worker 因上游 compact 服务故障中断，并非已取得完成结论；已按同一 worker 职责重新分派收口，不静默更换模型或将其 WIP 视为完成。全范围 smoke 仍在运行，本轮未提交生产代码。


### 2026-10-14 接续：Candidate F 本地解析与单色导入修复

- 已从原聊天恢复真实用户范围、OpenSpec 和 live handle；未将 PRD/已存在的 lab 当作 App 交付。使用 `paper-agent-desktop-app`，35 项任务中当前仅 6.1 已核销，其余仍按原范围待验收。
- 修复 disabled LLM 导致全部本地 Research 不可用的根因：新增 `ResearchLocalRuntimeSettings` 与 local-only composition，复用真实 ParsePaperUseCase/catalog/document/evidence/chunk/event/run-store 路径。导入仅接受已认证 owner 的授权导入目录；不创建远程 source、LLM、embedding 或 Qdrant，analyze/ask 仍明确不可用。完整 Research settings 对 disabled LLM 的拒绝保留，防止 guided 继承默认 provider。
- worker 冻结检查：composition 文件 22 passed；settings/guided-disabled/真实 auth+PDF 76 passed；architecture boundary 5 passed。最终 local composition/settings/auth PDF 定向 3 passed，guided+boundary 21 passed；真实 auth/PDF 集成又独立通过两次。API 集成使用测试生成的 selectable-text PDF 和禁止 socket，不能冒充真实论文或安装版验收。
- 主控以既有真实论文 PDF 做独立 parser 预检：747,542 bytes，SHA-256 `653b29619eae2ae4b361d7bdcdc06db4bedcb1b0eabe31814036beca8c1af0b1`；18 sections、90,521 body chars，确定性 selectable-text quality gate passed。这只证明该输入的 parser preflight，不证明 UI 导入/完整图表识别。
- ResearchSourceEntry 新增明确 `appearance="paper-agent"` 并由正式 App 集成；错误、处理中、缺少 paperId 的完成回执都保留真实状态，不伪成功；修复硬编码紫红逃逸，两主题的 danger/status 继承灰阶。默认 portal 配色独立保留。展示层 fixture 只验证生产组件 DOM/computed style，不作为业务成功证据。
- monochrome worker 全 frontend：167 files / 900 tests passed，typecheck/lint passed。App prop 集成后主控再次验证 3 files / 9 tests、typecheck、目标 ESLint passed。
- Electron inventory duplicate 校验改为 Set 线性检测，保留精确 manifest 契约、全资源安全检查及 checksum/runtime owner；增加早/晚重复、不安全路径、所有唯一文件仍检查和约 80k inventory 回归。主控最终 desktop typecheck、17 files / 77 unit tests、1 real Electron startup/sandbox/bridge/single-instance E2E passed。该 E2E 不覆盖真实论文业务窗口或安装。
- 已关闭上述两个实现 worker 与独立 desktop worker；当前源码/测试/config 3,923 文件已在 PRIVATE `candidate-f-source-freeze.json` 绑定，构建前复核六个 local-worker SHA-256 一致。中途手工核对 hash 字符串录入错误已纠正，不是源码漂移；后续全快照复查 drift 为零。
- Candidate F 首次 source build 与 1,536 MiB heap 的诊断重试均自然失败（npm exit 134）；PRIVATE 失败 stderr 明确出现 Allocation failed / heap out of memory。未忽略失败、削弱类型检查或复用旧产物。此前仅设计用途的 3107 开发预览在核对 PID/executable/command/birth 后被停止，释放约 6.9 GiB private commit；源文件/截图保留，既有真实运行实例未停止。随后以原 4 GiB Node heap 通过唯一 owner 正常重建；此条记录时结果仍待观察，不能视为 build 成功。
- 原完整 smoke PID 70144（父 scripts.dev smoke PID 66576，Python 3.14）仍有 live process，未取消/重启；它启动于本次修复之前，不能绑定 Candidate F 最终 gate。最终提交仍需冻结源码上的必需 `python -m scripts.dev smoke`、严格 OpenSpec 和真实候选证据；当前无新增代码提交、无安装包完成声明、无付费 provider 调用。
- 只读 explorer 自定义角色因运行时不支持指定 medium reasoning 被拒绝，未静默替换模型；主控自行继续只读核对。


### 2026-10-15：真实 PDF 导入成功与 reader 根因接续

- Candidate F 在清理临时设计预览后，经唯一 owner 正常 build / activate：release `20261014T151030Z-49604f19f2af`，source digest `49604f19f2af59499e3562c944c471ec358b3057d5aa9dfa6bc0febe3c19b174`，dependency digest `3d034553cd31caa50745e5857e5b2ace3201d052946895ee0393fb5b2c9e84c5`；正式 start 返回 ok=true/DEGRADED，api/frontend/bff/raw_rewrite/runtime 全部 healthy。本机服务可用不等于远程模型已配置/验证。
- 主控在新隔离资料根使用正常 UI bootstrap/login，无 cookie/token 注入；真实 history PUT 200、import POST 200，真实 PDF 转换完成，reader GET 200。输入为前述官方公开 PDF，不是生成 fixture；未进行任何付费模型或搜索请求。
- 添加论文后暴露真实 reader 崩溃：本地导入 `publishedAt=""` 导致 `formatPaperDate` 抛 RangeError Invalid time value。已从错误页面正常 reload 捕获 browser pageerror 与 reader metadata，保留 PRIVATE `candidate-f-real-reader-errors.json`；没有把 HTTP 200 当作 reader UI 成功。
- 日期 root fix 只改 formatter 与原测试，明确返回“日期未知 / Unknown date”，覆盖空值、非法值、日历越界、闰年和正常 locale；16 tests、typecheck、目标 ESLint/diff check passed。没有以当前时间填补缺失日期。
- 文件名 root fix 只改 ResearchImportApplication 与测试：从 owner 授权的已存 safe filename 给出诚实 fallback title，metadata 保留 `title_source=import_filename` / `import_filename`；缺失时沿用原 fallback，不补造书目作者/日期。原 owner/identity/fingerprint/recovery 不变；4 tests 与完整 compile passed。
- 原完整 smoke 自然 exit 1：4 failed、4522 passed、23 deselected；保留完整失败记录。其中出现 MemoryError、can't start new thread，以及在 transcript gate 失败后触发的 immutable conflict。资源清理后，项目 interpreter 两个完整失败关联文件 28 passed；同原 Python 3.14 对四个准确失败 node ID 4 passed。没有修改相应用例或断言、没有降低 50 个 actor 的并发。本结论仍不替代重新跑完整 gate。
- 精确 Windows process handle 的接续 helper 观察到原 smoke exit 1 后按规则拒绝自动授予资格/启动 final；回执保留 `previous_gate_failed_requires_root_cause_audit`。完成上述资源与 same-interpreter 审计后，另启 Candidate G 完整 `python -m scripts.dev smoke`，绑定新的 3,923 个 source/test/config 文件 freeze，PRIVATE 回执为 `candidate-g-final-smoke-receipt.json`，live exec session 28010。最终结果未取得，不提交生产代码。
- Candidate F 已通过唯一 lifecycle owner drain/STOPPED，原隔离账号与导入资料保留。当前 Candidate G 唯一 owner source build（exec session 57243）正常运行，等待 production build 回执及真实 reader/笔记重开复验；不得将 Candidate F 已暴露的坏 UI 当成修复通过。


### 2026-10-15：Candidate G 真实阅读与 portable Doctor 长路径阻塞

- Candidate G source release 正常 build/start：`20261014T160620Z-b2d933ba0487`，source digest `b2d933ba048791cc78349a35865f74d2dd4a29f993f1eefdcb76c72a2af8b112`；同隔离 owner 正常登录，重新导入真实 PDF、加入资料库后显示“已保存”，reader 展示真实 18 页提取文本、文件名标题、日期未知与作者未提供，无 browser pageerror。此为 source production BFF，不是安装宿主。
- 笔记验收未取得结论：一次 REPL 检查错误使用 `role=button` 定位实际上 `role=tab` 的“笔记”，超时后 kernel reset；未发生笔记写入，实际持久化 note 仍为空。未将观测超时或错误测试 selector 当作产品保存成功/失败。已按实际 DOM 修正 PRIVATE 有界验收脚本，尚待可用 managed runtime 执行；密码只生成于验收进程内，没有落盘或手工读取/注入 cookie。
- Portable stage 成功：bundle `desktop-c9bcb98b4cfc4f51e3d936ee`，79,960 resources；独立 Node `validate:runtime` 对全部 inventory/type/path/size/hash 验证通过，没有缩减清单。
- 第一次人工 managed CLI 忘记 -s/PYTHONNOUSERSITE，按安全合同被拒绝；该人工调用还生成三条未登记 bytecode，下一次完整 inventory 正确拒绝 extra_count=3。已核实只删除此调用生成的三条精确 .pyc（owned stage 内、非 reparse），未改 manifest/资源校验。正式 host 本来就使用 -B 与隔离环境，此处是人工验收参数问题，不是放宽安全边界的理由。
- 随后正确隔离的 managed start 已 materialize/activate，但自然失败 `desktop_doctor_blocked_start` / `desktop_active_release_invalid`。主控逐资源定位 Doctor 的第一失败：resource index 5529、absolute path 266 字符，普通 is_file/read_bytes 看不见已存在的长路径；windows_extended_path 下文件存在且 size/hash 与 manifest 完全匹配。同受控配置下完整唯一 ReleaseManager.verify 返回 VERIFY_OK。证据为 PRIVATE `candidate-g-doctor-resource-diagnostic.log`、`candidate-g-doctor-longpath-proof.json` 和 `candidate-g-managed-release-verify-diagnostic.log`。
- 根因是在 Doctor 自行重复检查中仍使用普通 Windows 文件 IO，不是包缺资源。已按 worker 角色分派仅 doctor.py 与对应测试的内部 extended-length IO 修复；保留完整 type/path/hash/size 和 junction/missing/corrupt 拒绝，不用短 data root 或修改系统长路径策略掩盖问题，不改既有 stage/release 产物。
- Candidate G 完整 smoke（session 28010、runner PID 57348）仍 live；它晚于 reader 修复、早于新的 Doctor root fix，不能绑定后续最终 release。若 Doctor source 变化，G freeze 的 drift 必须如实保留，再在新 freeze 后接续最终 gate，不中止仍活着的 G。


### Candidate H：Doctor root fix 冻结与受控接续

- Doctor 只将 release resource 内部 is_file/read_bytes 切换至 windows_extended_path，保留同一完整 manifest、reparse/containment/type/size/hash 检查和 public 正常路径。真实 Windows >260 checksum 正例、同尺寸篡改、缺失与 junction 负例均通过；worker 16 passed，完整 compile passed。主控整合 Doctor 与 App runtime 文件复跑 26 passed in 3.44s。
- Candidate H 新 freeze 已绑定同一 3,923 个 source/test/config 文件，PRIVATE `candidate-h-source-freeze.json`；唯一 owner source stop/build 流水线 live session 34329。没有改动旧 Candidate G portable/stored release 来伪装修复，不预先报告 H 构建成功。
- 新完整 gate 接续 helper（live session 45076、helper PID 61632）已取得并验证 Candidate G runner PID 57348 的真实 Windows process handle、精确 birth FILETIME 与 executable，等待其自然终止后才有资格启动 H。若 G command 非零则停止并留失败回执；若 H source drift 则拒绝 gate；H 自身完整 smoke exit 0 且前后 source 无漂移才标为 passed。G 的 source drift 会如实留下，不能当作 H 的最终资格。
- 当前 OpenSpec strict valid；发布 AC 仍未额外核销，未提交待完整 gate 的生产代码、未生成最终安装交付、未启用远程付费模型。下个实际步骤是核对 H source build 回执，经唯一 owner 重新 stage/bind 后运行已准备好的真实 UI/PDF/笔记持久化与重开验收脚本，再推进实际 native 业务窗口和安装。

### 2026-10-15 01:10 接续检查点：Candidate H 与后续 reader/identity 修复

- Portable Doctor 长路径修复已通过 16 项定向用例；主控 Doctor/runtime 合并回归 26 项通过。修复仅在严格 containment/reparse 检查之后使用 Windows extended-length 文件 IO，未跳过 inventory、size 或 hash。
- Candidate H source release `20261014T164523Z-79e224480204` / source digest `79e224480204c252ef90c773305d96843c8753285a0a2a55dba49a5c0724d799` 已构建；bundle `desktop-0375f812712aa5ba5a917530` 已真实完成 portable stage。隔离根 `pa-20261015h` 的 managed startup 仍 live，不能提前写 ready 或安装通过。
- 真实 development Electron 的隔离 runtime 已单独构建为 `20261014T165243Z-79e224480204`，同一 source digest；仅用于真实窗口/普通登录/实际文件/笔记回读证据，不是 installed 或 packaged 验收。测试 helper 的启动等待需与真实 runtime 有限上限一致，业务操作仍短时有界。
- Candidate G 完整 smoke 仍自然运行，已进入 71% 附近；G freeze 之后已有 Doctor 修复，因此即使命令成功，也不能认定冻结输入通过。H 的接续 helper 会在前一真实 handle 结束后校验源漂移，不重写 freeze 来掩盖变化。
- 阅读器审查发现有可解析文本时缺少 PDF 原件切换，页 locator 只更新 notebook 而未切到真实 PDF。正在以既有 owner/sourceRevision/revision 工作区合同补齐文本/PDF 模式与持久化，不增加 renderer 业务存储。
- 同一真实 PDF 在不同 import request/path 下产生不同 paper identity，正在补齐 owner-scoped 内容身份去重。不同 owner 和不同字节不得合并，文件名不得冒充论文身份。
- 以上后续生产修改将形成新的 Candidate I；H 的不可变资源可继续验证历史检查点，但其 source freeze、bundle、测试不得冒充最终新代码。35 项任务仍仅 6.1 完整勾选；当前未生成或宣称最终安装交付。

### 2026-10-15 01:30：完整 smoke 终态与真实 native 反例

- Candidate G 的完整 `python -m scripts.dev smoke` 命令自然 exit 0：`4526 passed, 23 deselected, 61 warnings in 4483.95s`，后续 deterministic AgentLoop（network_calls=0）与 source validation 也通过。日志 SHA-256 为 `026645d6e907bcf969adaabbd930a0deaaa837b932ed1445698fae6151206c17`。这证明此前资源故障已消除，但 wrapper 仍正确标为 failed：Doctor/reader/identity 等修改发生在 freeze 之后。H handover 自然拒绝 `source_drift_before_gate`，没有重复起第二个 smoke。
- 真实 development Electron H 已通过：原生宿主启动、绑定隔离 3120/8120、普通账号初始化、用户文件选择器、真实 PDF 解析、资料库与正文画面；日期未知未再崩溃。这不是 installed/packaged 验收。
- native helper 在 library 已显示“已保存”后立即读取真实 BFF history，却尚未看到 material，正确拒绝继续。只读检查稍后真实 history revision 3 已包含该 PDF，表明是同步确认时序而非格式/身份错误。已定位 App 在没有本地 saveReceipt 的导入路径无条件使用 confirmed；必须在真实 workspace 同步前保留待保存/失败，不能为了 helper 加 sleep 掩盖虚假成功提示。
- native helper 本次业务结果为 failed，笔记与记忆步骤未执行。其 close 观测也未确认，不能宣称安全退出验收通过；随后实际 owner 日志自然记录 17:23:09Z stop_requested → 17:24:13Z stopped → host_exit，无任意进程强杀。保留原始失败回执，不追写为通过。

### 2026-10-15：进入 Candidate I 前的 reader/import 根因回归

- Reader 支持真实文本/PDF 模式；页引用改为等待实际 viewer 确认页码与页数后成功，18 页文档请求第 19 页明确失效；加载失败不保留假定位成功。修订切换重置旧 locator。稳定 callback 与同页 no-op 防止 PDF ready 触发循环 workspace 写入。reader/viewer/workspace 目标 Vitest 35 项、Python contract 26 项通过；PDF chrome 的 opt-in 黑白主题仍在最终核对，默认 Portal 视觉不改。
- 本地完整材料使用 actor scope + 实际 bytes SHA-256 建立身份：同 owner 重命名/重新导入/重开复用 canonical ID；不同 bytes 或 owner 隔离；receipt/run/source snapshot 仍独立。真实 747542-byte 论文在同 owner 五次导入保持同 ID，另 owner 独立；两个独立 OS 进程顺序重开与单 runtime 并发均实测。既有重复记录不静默迁移，以免破坏外键；这不是新 compatibility layer。
- Identity/import/API 35 项，相关 domain/source/认证 96 项，architecture 256 项通过（worker 非重复合计 387 项）；compile/strict 通过。完整 task 4.2 尚未核销。
- 无 saveReceipt 的导入/已有资料现在必须等待 owner 匹配、workspace ready、synced 且 clean 后才显示已保存；失败重试 retained workspace draft，不重新导入或解引用缺失 receipt。有 receipt 继续保留 revision/generation/snapshot 强校验。目标 7 文件 63 项、显式覆盖 feature 的 TypeScript 检查及 lint 通过；真实 native 重验尚待新构建。

### 2026-10-15 02:15：完整输入冻结 J 与检查终态

Candidate I 的额外显式 feature TypeScript 检查发现三个测试 fixture 类型错误（settings stopped literal 过窄、Testing Library 不支持 exact、ReaderAssistant Notebook/callback fixture 不符合真实合同）。已修复测试根因，未修改 production 或 exclude，未削弱断言。I 原失败诊断/receipt/freeze 不覆盖，I 完整 smoke 保持自然运行，J 会在验证过的同一 PID/birth/executable 终态后接续且再次拒绝输入漂移。

- Candidate J：4414 个 tracked/non-ignored untracked 输入（覆盖新增路径、spec 与测试）；仅排除该 change 的生成证据文档。freeze SHA-256 `fa9f21c5857397bda9f8cded3cb32bf2a87cb662be5ca7671d1d9a4b66bb13b3`，捕获时间 `2026-10-14T18:10:39.935563Z`。当前 added/removed/changed 及 HEAD 漂移均为空。
- J 全前端 172 文件 / 1006 项通过；正常 typecheck、显式 feature/test typecheck、全 lint 均 exit 0。主控 backend/import/workspace/Doctor/runtime 87 项通过。
- Desktop 全部 17 文件 / 77 项、typecheck 与真实 Electron startup/security/single-instance E2E 1 项通过；这项 E2E 不冒充业务窗口或 installed 验收。
- J 独立 source build 进行中；I 历史不可变 source build `20261014T180413Z-2ca54fdc7345` 的 routes-manifest 已证明 local-desktop raw external rewrite 为空，真实 catch-all route 存在，但端口重绑定与业务闭环仍待最终包实测。
- 最终 gate、真实 J 笔记/个人记忆/PDF 续读、portable 重绑定、installed 环境和 provider/预算条件仍保留，未提前提交或宣称最终交付。

### 2026-10-15 02:55：真实 portable 本地闭环通过，PDF transport 修复冻结 K

J portable 使用被管理 Python 3.12.14/Node 24.21.0/npm 11.19.0 完整清单，bundle `desktop-13fccacb349097386c8fd924`。实际将构建端口 3120/8120 重绑定为 3124/8124，所有 API/frontend/BFF/raw/runtime readiness 通过，真实 state 为 DEGRADED（remote 未配置），不是 READY/provider 验证通过。先前 staging 使用不同 Node PATH 曾正确拒绝 dependency drift；改回同一已验证工具链重新调用唯一 build owner，未改 manifest。

在这个不可变包上已执行普通 UI 的真实本地数据闭环：
- 正常本机账号创建、真实 PDF 导入和文本阅读；同 bytes 再次导入后可见/持久化资料库均只有一条且 material ID 不变；
- 笔记 UI 提示保存后真实 BFF GET 200 精确匹配 paper/sourceRevision，页面重开回读一致；
- 用户手动记忆 candidate 真实持久化、UI 确认后 active，再停用；刷新重开仍 disabled r3，远程 policy 明确 false；
- 黑白工作台截图和真实 API 回执已保存，pageerror 与浏览器外网请求均为空。

此 evidence scope 明确排除已知原件 PDF transport 失败，不替代 native/installed 或最终全闭环。K 已冻结新的严格同源私有源 transport：4414 输入，freeze SHA-256 `963beada079bf9693de98e4470a516c3fc3ba7bab7e3009591a5fc416fac0984`。目标 viewer/source/contracts 120 项、普通与显式 feature typecheck/lint 通过。K production build/fullfrontend 进行中，真实 PDF/续读和最终完整 smoke 仍待终态。前一 I smoke 继续自然运行，J handover 将正确拒绝漂移，K 只在验证前一 command 自然通过并再次确认完整输入无漂移后运行，不修改旧 freeze 或强停旧测试。

### 2026-10-15 03:05：冻结 K 原生真实 PDF 通过，完整验收仍未通过

- K source release `20261014T185240Z-3c1c3993b69d`，source digest `3c1c3993b69d92dfa02a9c3999e093beda8c20034093658b22d750888d6fe642`；全前端 172 文件 / 1041 项、普通与显式 feature TypeScript、lint 全部通过。
- K development Electron 经正常账号 UI 和真实文件选择器验证：私有 PDF 原件同源传输成功，真实 18 页、第 2 页 canvas 595×841；刷新恢复 PDF mode / page 2，笔记精确匹配 owner 工作区回执且重开保持。黑白真实窗口截图已保存，无 pageerror、console error 或外网 browser request。
- 整轮 native helper 仍 exit 1：多操作组合的个人记忆步骤超过 15 秒总体预算，实际 create/confirm/disable POST 均 200，随后定位/刷新过程尚待独立诊断；不得据此认定完整记忆 native 验收通过。失败 receipt 和截图保留。原生 owner 已确认正常 drain / stopped / host_exit，未 fallback kill。
- 完整 portable K bundle `desktop-9c4994dd76dcacdfc369d984` 已 staged。J portable 已正常 drain stop 并保留数据；K 使用独立数据根开始实际 3124/8124 重绑定。首次 CLI timeout=600 正确被有限预算合同拒绝，保留失败日志；重新使用允许的 300 秒预算，未改 timeout 合同。
- I 历史完整 smoke 自然运行约 73%，其晚续 source drift 仍不可冒充最终 gate；K handover 独立检查前一终态与冻结输入后才允许运行。未生成最终安装版、未提交完成代码、未核销完整 tasks。

### 2026-10-15 03:20：K portable 真实闭环通过，备份暴露新的合同缺陷

- I 历史完整 smoke 命令自然 exit 0：4528 passed, 23 deselected, 61 warnings in 4473.45s；AgentLoop deterministic/network_calls=0 与 source validation 随后通过。其 wrapper 正确拒绝 freeze 后五个文件漂移，不能作为冻结最终 gate。J handover 正常拒绝，K 在完整输入再次确认无漂移后于 `2026-10-14T19:17:59Z` 接续唯一完整 smoke（runner 61012），未并发启动第二个。
- K portable 实际绑定 3124/8124，API/frontend/BFF/raw/runtime health 全 true，DEGRADED 表示远程未配置，不是联网能力通过。独立完整 UI 成功：正常创建本地账号；真实 PDF 导入与同 bytes 再导入只有一个 canonical/material identity；真实 18 页 PDF 和第 2 页 canvas；笔记真实持久化及刷新；PDF mode/page 2 刷新恢复；个人记忆候选/确认/停用后刷新仍 disabled r3，remote policy false。pageerror 及 browser 外网请求为空。此为 portable production BFF/browser scope，仍非 installed Electron。
- K portable 输入 79972 个资源、1936273519 bytes，完整 manifest/size/hash/未登记文件校验通过。CLI 先前非 bundled interpreter 被正确拒绝，随后实际使用包内 Python -B -s，未放宽 isolation。
- 真实 J 数据备份验收 exit 2：`desktop_inventory_required_owner_missing` / `research_rag_local`，缺少从未启用的 `research/catalog/chunks`。backup 未产生有效 ZIP、未进行 restore/activate；23 个源审计文件 hash/size 前后不变。不得制造空索引目录掩盖问题。正在根因修复 local-only runtime 与 required storage contract 的差异，已存在/启用 RAG 的完整性验证保留；完成后必须新建 L 冻结，不能继续宣称 K 最终通过。
- K native 后续独立 helper 因复用 root 的 STOPPED startup projection 拒绝；实际本次启动还是旧状态瞬态仍在审计，不先删终态条件。原失败 evidence 与正常 owner shutdown 记录保留。
- 独立 candidate packaged directory 首次下载 Electron 网络 timeout，保留失败日志；后续使用已存在且实际验证版本 44.7.1 的 Electron distribution，未改生产配置或跳过 runtime inventory。候选目录不是最终安装构建或安装验收。

### 2026-10-15 03:30：候选打包目录已构建，但完整包验收正确拒绝

使用同版本已验证 Electron distribution 44.7.1 构建独立 packaged directory，electron-builder exit 0；产品 Agora Paper Agent 0.1.0.0，实际 Authenticode=NotSigned，不能把日志 signing with signtool 当作已签名。EXE SHA-256 `b48f3f9361fada8ceb020c366a10e32752cc28942532173dbaf64df90a995068`，app.asar SHA-256 `38da9f8cab167b33bf2e2d6468d34acaac52037c7cee65d2514476a0e1adb3b2`。

主控对真正打包输出重新执行同一完整 runtime validator，正确失败：`resource frontend/node_modules/cssstyle/lib/generated/.gitkeep is missing`。进一步精确 inventory 差集发现 146 个缺失文件、0 新增，包含 hidden/pyc/长路径第三方 license。未启动该包、未修改 stage/manifest、未把 builder exit 0 视为包装完整，更未删除资源或许可来通过检查。正在对生产 packaging copy 和最终验证钩子做根因修复，并保持严格 owner/size/hash/extra-file/reparse gate；需新 L freeze 后复验。当前其他 local-only backup 合同修复也进行中，K smoke 将保留为实际历史检查，不虚称无漂移最终通过。

## 2026-10-15 04:15 集成检查点：包库存根因、原生剩余动作与未完成范围

- 修正早期包差异计数：普通 Windows 路径扫描得到的“146 缺失”含长路径误判；对 namespaced path 完整库存与逐文件 hash 复核，实际缺失为 **22 个 `.gitkeep`**，其余 79,950 个资源全部匹配。根因为 builder 的 glob walker 在 matcher 前忽略这些文件，不是 bundle manifest 错误。
- 生产打包改为 manifest-owned 的独立、排他复制（非硬链接），拒绝已有目标、source/destination 别名重叠、reparse、未列出或被修改的文件，并在复制、签名后与最终 artifact 阶段进行完整 gate；不重写 manifest、不修补旧包。匹配检查为 desktop 18 files / 86 tests 及 typecheck。
- `.tmp/paper-agent-desktop-app-20261014/candidate-l-packaged-dir-local-electron/win-unpacked` 已由实际 builder 构建并通过 79,972/79,972 资源、1,936,273,519 bytes 的完整校验。**该目录名含 l，但 payload 仍是 K，证明范围仅为打包根因修复；不是重新冻结的 L release，也不是安装/签名验收。** Python/host EXE 当前未签名；Node 保留原有有效签名。
- 原生 development host 的剩余交互已实际通过：18 页真实 PDF 的第 2 页和 exact source 恢复、1.25 zoom、Ctrl+K、本机响应式 drawer、专注往返、真实 history/lifecycle 一致；最终 zoom 恢复 1。该运行无 page/console error，精确 owner PID 44272 由正常 drain/STOPPED 退出，3120/8120 已释放。此证据仅补充剩余 unpackaged 动作，不冒充整流/packaged/installed、物理输入法或电脑重启证据。
- native v4 诊断已单独确认 memory disabled revision 3、默认不外发策略及正常 reload 后保持；其整体失败是 helper 在 PDF 保存回执之前读 history。后续 helper 使用真实 PUT ACK 与精确 GET，旧失败证据保留，不调整生产业务状态来让 helper 通过。
- 目前仍欠缺 PRD F-08 / task 7.3 的独立、明确确认的数据清除入口；正在补齐 owner 受控停止、预览身份绑定、有限清除、凭据/会话和失败安全合同。默认 packaged profile 尚未创建，不能先污染用户默认根再用手工删除冒充产品能力。
- K 全量 smoke 仍自然运行，未取消/并跑；之后的 recovery/packaging 修复会使冻结后 wrapper 如实报 drift。即使命令最终 exit 0，也只能作为该运行证据；最终代码仍需重新冻结并通过完整 smoke。联网 provider/来源/费用授权、真实问答/报告导出闭环、失败场景、备份恢复迁移、安装升级/卸载、电脑重启等范围未核销，最终目标尚未完成。


## 2026-10-15 04:34：K gate 终态与后续实施

K 全量 smoke 已自然结束，session 50991 terminal：实际测试命令 exit 0，pytest `4528 passed, 23 deselected, 61 warnings in 4422.01s`；deterministic AgentLoop `network_calls=0`，source validation valid、0 errors、0 warnings。日志 SHA-256 为 `0b17e3cdf561d4d1d5092ad6323638fcc0dc4cb61c80d1adec10e388acb13f68`，主控重算匹配。runner PID 61012，helper PID 67560；未取消或并跑第二个全量 smoke。

冻结后检测为 added 7 / changed 12 / removed 0，HEAD 未变；wrapper 正确返回 failed/2，handover 不继续启动下一 gate。该结果仅证明本轮命令通过，**不是最终源码 gate**。L freeze 尚未捕获、L gate 尚未启动，必须先等 production 修复与安全清除实现完成。

主控已补 desktop-shell 清除 requirement / design D10，strict 校验通过，并由三个互斥 worker 实现 Python owner/finalizer、Electron bridge/session/原生确认、设置 UI。旧 profile 不作实际删除测试；只清除本轮新建的受控 fixture。另有独立 worker 处理实际 125% 截图里的 PDF toolbar 裁切，避免把外层 overflow hidden 当无布局缺陷。完整范围见 `acceptance-gap-audit-20261015.md`，没有把后续工作缩减为安装验证。

Recovery 的 current-source + actual J data 隔离 restore 已返回 VERIFIED，但第三轮发现源 memory SQLite 字节审计变化，仍在定位 WAL/checkpoint/helper 路径；不伪称 23 个源文件始终未变，失败回执原样保留。该证明既不激活恢复，也不操作 K 的 live 实例或账号凭据。
