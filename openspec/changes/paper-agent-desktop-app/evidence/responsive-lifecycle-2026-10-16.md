# 2026-10-16 启动与退出修复证据

## 范围与当前状态

本轮直接处理用户报告的打开慢、关闭长时间等待和退出异常后重开失败。采用既有 `paper-agent-desktop-app` 变更；不宣称第 1–9 节所有任务已完成，也不把建议性能预算当作已获用户确认。

基线提交：`1807ed6d7302f22b31b9b1eed773a60f8ea8bce4`。本轮代码和回归由三个分离写集的 worker 完成后，再由主控统一冻结候选并执行检查。并行工作目录的 foreign tracked files 已记录 hash，不纳入本次提交。

本文件先记录可复核基线；新候选检查与实测结果须在完成后追加，不使用旧结果冒充通过。

## 已观察的旧版本行为

- 本机实际安装版本为 0.1.1；安装入口为 `%LOCALAPPDATA%/Programs/Agora Paper Agent/Agora Paper Agent.exe`。
- 升级后一次完整启动从本地时间 2026-10-16 06:32:45.833 到 06:36:07.001，约 201.2 秒；另一次重开从 06:59:40.295 到 07:01:40.278，约 120.0 秒。后一次 API/前端服务约在 07:01:27 才创建，说明主要时间不只在服务健康等待。同期运行过完整 smoke，不能据此分离纯硬件、杀毒、磁盘和并发负载影响。
- 旧 runtime manifest 为 79,937 个文件、约 1,846.0 MiB；其中 Python 包含默认桌面能力之外的大型本地 ML 依赖，前端包含开发依赖。此统计不包含 Electron 与 active release 副本，不能当作完整安装占用。
- 一次退出从 06:46:46.730 到 06:47:53.530，约 66.8 秒。
- 17:09:33.732 请求停止后，17:10:33.519 出现 renderer `killed`，随后 CLI 响应被判 `runtime_protocol_invalid_json`；17:10:49.845 重开得到 `desktop_process_ownership_incomplete`。这些是相邻事件，不足以单独证明 renderer 被杀的唯一原因。
- 17:12:24.594 记录 runtime stopped；17:48 只读观察未见已安装 App 进程，instance 为 STOPPED、进程列表为空。

原始本地记录保存在 `.tmp/paper-agent-lifecycle-fix-20261016/` 的基线与收据中，日志不得包含密钥或业务正文。

## 根因与不可退化边界

1. 旧 Windows 后台服务使用 `CREATE_NO_WINDOW`，退出路径仍尝试 console control 信号，并对各进程串行等待完整宽限期；失败或不适用的信号会把退出拖到超时。修复必须先业务 drain，仅按创建时间与可执行文件核实的精确进程身份收敛；不能按名称结束进程，也不能向其他 console 进程广播。
2. 旧宿主 stop 等待整个 pending start 完成，缺少协作取消。修复需经私有有界 stdin 控制，在验证、复制和创建进程的安全检查点停止后续启动；取消送达不等同已停止。
3. 减少重复工作与依赖体积必须保留同口径完整资源哈希、reparse 拒绝、配置/依赖/release 身份。不得以 mtime、marker、抽样、后台补验证或隐藏下载代替 gate。
4. 未保存内容、活动任务、renderer 状态未知的原生退出确认和失败保留窗口必须保留；本轮不自动替用户确认退出框。
5. 最终自包含 Windows 安装程序仍不得要求用户安装 Python/Node 或首启运行 pip/npm。默认不带本地 LLM/OCR/reranker 的能力合同不能导致真实文本 PDF、授权、本地资料、笔记、记忆或已保存成果不可用。

## 新候选检查与交付

分阶段结果与失败历史见下文。r9 的范围回归、strict、完整 smoke、真实服务/BFF 启停与取消、精简 bundle/installer 构建均已通过；真实用户窗口、无开发工具隔离 Windows 和完整业务/模型闭环仍不是已完成验收。安装升级与提交结果由最后的收尾节记录，不使用中间候选背书。


## 中间候选 r6：构建与真实启动失败（不交付）

全部当批代码与回归先落盘，再冻结独立 `candidate-r6`（base `1807ed6d7302f22b31b9b1eed773a60f8ea8bce4`，4,426 files，freeze SHA-256 `6f59c4a9a215732411a477935e3d10c6a69c829fab850b2738017c23e4593ad9`）。候选为普通独立依赖副本，不使用共享软链接或硬链接；冻结后未修改源码。工作区 foreign tracked 40 files 和既存安装必需输入分别保存 hash。

该中间候选通过了 Python 定向回归 314 passed / 3 skipped、桌面端 171 tests / 24 files、前端 1,187 tests / 170 files、相关 typecheck/lint、native isolated Electron E2E 1 passed、OpenSpec strict。更早摘要中的 1,220 前端用例含其他工作，不能当作本冻结候选测试数。源码生产构建、独立精简 Python 构建、完整 portable bundle 与 Windows installer 构建也通过。

r6 的 manifest 包含 14,458 resources；连同 manifest 共 14,459 files / 412,713,494 bytes（约 393.6 MiB）。Python 195,816,395 bytes，前端 65,848,023 bytes，Node 106,986,871 bytes。Windows installer 为 `Agora-Paper-Agent-0.1.2-x64.exe`，289,416,824 bytes（约 276.0 MiB），SHA-256 `cb65ea9e6623a09d2c711c24040725a44ed32d339660e4b84502b3405076d9f5`，实际 `NotSigned`。这些数字只说明中间候选的构建体积，不代表最终交付、首装或性能验收通过。

2026-10-16 22:14:09 至 22:14:36，本机以 portable Python 对全新隔离资料根和 `127.0.0.1:3136/8136` 执行真实 CLI start，27,218 ms 后失败，响应 `desktop_doctor_blocked_start`，未创建服务。doctor 的 `frontend_lock` 仍要求开发目录 package-lock，而 `production_release` 仍强制旧 Next bin 入口，均未识别已封存 standalone 运行形态。因此该候选不安装到用户机器、不宣称启动修复成功；其必需完整 smoke 被明确标记 superseded，并精确核对进程身份后停止，不记为通过。原始失败 stdout/stderr/JSON、构建收据与受控停止收据保留在同批 `.tmp` evidence 目录。

后续须修复诊断器与 standalone 合同的真实衔接，保留完整资源、依赖身份和未知输入拒绝；重新冻结并完成所有 gate。不得通过补造开发 lockfile、关闭 doctor 或修改 r6 的已封存 manifest 绕过此失败。


## r9 源码冻结与阶段验收

修复 standalone doctor 衔接后，回归又暴露 materializer 对长路径混用普通 `resolve(strict=True)` 和 extended I/O 的真实问题；已统一 materialization 的 Win32 extended-length 路径，未知 reparse tag 仍在解析前拒绝。doctor 现在复用 `ReleaseManager.active()` 完整验证，并只在同次 `run()` 共享结果；没有跨启动信任缓存。r7/r8 的失败收据保留，不当作通过。测试中注入未知 reparse 元数据及旧 doctor 方法签名的 double 已对齐真实接口，原拒绝断言保持。

最终生产候选（目前等待必需 smoke 最终结果和真实升级检查）：

- `candidate-r9`，base `1807ed6d7302f22b31b9b1eed773a60f8ea8bce4`；源码 freeze 4,426 files / 42 owned paths，SHA-256 `bafe5266ee6ec8f46bf872e46839b045f583612cb5da4024575400752d683467`。
- Python 定向回归 **338 passed / 3 skipped**；桌面 **171 tests / 24 files**；前端 **1,187 tests / 170 files**；相关 lint/typecheck/build、native isolated Electron E2E **1 passed**、strict 均通过。
- 精简 Python profile 真实离线组装、源码生产构建、portable bundle 和 Windows NSIS installer 构建通过；完整资源验证在 afterPack/afterSign/afterAllArtifactBuild 保持一致。
- Bundle `desktop-a63c674805ae239f6e559d9c`；bundle manifest SHA-256 `e63236ffb142fb09e126ac24309f8c06c6bb0e63154cb0d2d182f2457c8200b1`。
- 安装程序 `Agora-Paper-Agent-0.1.2-x64.exe`，**289,416,840 bytes**，SHA-256 `782fdac3295117eed8dda44ced37c3b4d62c142e2845169993fc5e38c3072b9f`，实际 **NotSigned**。无 GitHub push/Release 发布。

### 真实服务生命周期（隔离 profile，不等同用户窗口冷启动）

`runtime-lifecycle-evidence-r9c` 于本地 2026-10-16 22:46–22:47 使用同一 bundle、全新隔离 `runtime` data root、portable Python、3137/8137 端口验证，严格单 JSON、实际 Win32 创建时间/可执行文件 ownership、release/binding identity、API/frontend/BFF/raw rewrite 全部健康通过；停止后两个 endpoint 关闭、记录 STOPPED/[]、原进程均退出。

| 操作 | 完整 CLI wall time |
| --- | ---: |
| 首次 start（包含完整校验与绑定） | 30,188 ms |
| 全部自有进程退出后重新 start | 21,227 ms |
| 第一次空闲 stop（包含 drain） | 2,460 ms |
| 第二次空闲 stop | 2,477 ms |
| 立即 preactivation cancel-start | 1,769 ms，exit 4 / `desktop_start_cancelled` |
| cancel 后真实 stop 核实 | 2,168 ms，STOPPED |

start/status 都真实返回 DEGRADED：本地五项 transport health 可为 true，但未配置远程模型的 doctor 仍降级，不能据此声称远程业务合格。driver 最初错误要求 DEGRADED 必须 runtime=false、错误要求早期异常带 storage_mode，已按真实生产协议修正；四项 transport、身份、ownership、退出与精确取消 code/state/exit 断言未删除。另一次 driver 人为清空 OS PATH 导致 npm.cmd 的 Windows DOSKEY 本地化 stderr，已仅恢复 SystemRoot/System32（不继承开发工具 PATH），不放宽 UTF-8 gate；失败记录均保留。

以上只是两个 start、两个空闲 stop 与一次立即取消的本机隔离样本，同期有构建/完整 smoke；不是系统冷启动、正式窗口可操作或忙任务退出的 P50/P95，也未宣称达到待确认的建议预算。late-checkpoint 取消由定向回归覆盖，未声称已全部真实采样。

### 资料能力与其他边界

精简 Python 的真实 PDF 创建/提取、SQLite、TLS、API/Auth imports probe 在 profile 构建中通过。另行隔离端口3136/8136的真实 BFF bootstrap/logout/login/session/desktop owner binding 已通过；后续 PDF 上传验收因测试 driver 生成的 textbox 超页而在上传前失败，未记为业务通过，测试服务已用真实 owner stop 收敛。没有改动用户账号或调用模型、远程来源、embedding；未据此核销完整 PDF/笔记/记忆产品闭环。该扩展验收仍待完成，不通过跳过授权或伪造业务数据消除失败。

全量 stage 资源在启停后仍逐文件完整核对：14,458 resources，无额外项、缺项或 hash 漂移。源码冻结/工作区精确代码、40 个 foreign tracked files 和7个原样纳入的安装必需输入核对无漂移。最终文档证据回填与任务勾选不改变已冻结生产代码；最终提交前仍需复核。


### r9 必需完整 smoke（最终冻结生产代码）

`python -B -s -m scripts.dev smoke` 在 `candidate-r9` 从本地 **2026-10-16 22:38:50.779** 运行至 **2026-10-16 23:54:37.009**，exit 0。包含 compile、Harness/Research/API/service/composition/architecture：**4,275 passed / 23 deselected / 61 warnings**，pytest 用时4,529.31秒。既定 `not live_research_e2e` 规则未变；没有跳过本轮失败或削弱断言。随后 deterministic AgentLoop succeeded（network_calls=0）与 sources validate（is_valid=true / error_count=0 / warning_count=0）均通过。完整 gate 日志 SHA-256 **`7a2d79fc78fa55213fe76b04a4bb75ccaade0dc5354189805696ef7e4e87e604`**。

本轮最终测试数以该 r9 收据为准，不重复引用 r2 的4,286项或其他工作区的前端用例数。候选的生产和测试代码冻结之后未再修改；后续只回填文档及提交收据。


## 本机 r9 原位升级（历史收据）

2026-10-16 23:55:23.860–23:57:12.656，在确认已安装 App 无进程、旧 instance 为 STOPPED/[] 后，以 r9 installer `/S` 原位升级，exit 0，用时108,797 ms；没有强制结束用户进程。安装入口保持 `%LOCALAPPDATA%/Programs/Agora Paper Agent/Agora Paper Agent.exe`，实际文件版本0.1.2。安装后的14,458个runtime资源逐一核对与r9 envelope一致，无缺失、额外或hash漂移。升级前所记录的43个非缓存/非release资料文件均存在且字节hash完全不变。没有启动真实GUI、操作用户账号、替用户确认原生退出、清空资料或重启系统。

## Git 可重复检出的最终候选 r11

提交前检查真实 Git blob 时发现：新 profile 的 requirements/constraints 固定 hash 最初绑定了工作区 CRLF；仓库 `.gitattributes` 强制 LF，会导致独立 clone 后不匹配。已将本轮文本统一为仓库规定 LF，并重算 profile 的两个真实文件摘要，包名/版本/能力集合不变。Git whitespace gate 另外发现3个既存安装输入的多余末尾空行，已仅移除这些空行。相对 r9，Python AST 一致；既存7个安装必需输入保持执行文本不变，其中换行/EOF格式化明确记录，不能再声称这些输入的原始字节hash完全不变。其余40个foreign tracked files原始hash仍未改变。

未修改旧冻结候选，而是重新冻结并构建 r11：

- `candidate-r11`，base `1807ed6d7302f22b31b9b1eed773a60f8ea8bce4`，4,426 files / 42 owned paths；freeze SHA-256 **`057fc7dfcd5506f588d412e5ff9dc201f6931f86d55792f42f630583d2436e9a`**。
- 已实际 `git checkout-index --all` 导出，导出4,426个文件与r11 freeze逐字节一致；requirements/constraints 的真实 Git blob hash 与 profile 引用完全相符，42个暂存路径与冻结字节相同。
- LF constraints SHA-256 `5d584cc7ed40a16d6c4b88f38a3ec38caf447b20cac7c08bca66e785d003682d`；requirements SHA-256 `225811399ca99a0d4d10cbe23873591be6a343eb60f19ccfa0165c304631b8a2`。
- Python定向 **338 passed / 3 skipped**、桌面 **171 tests / 24 files**、前端 **1,187 tests / 170 files**、typecheck/lint/build、native isolated Electron E2E **1 passed**、strict均通过。r11完整smoke单独运行，不能用r9通过替代。
- 默认builder下载Electron时出现 `connect ETIMEDOUT`，保留失败收据。随后使用builder正式 `electronDist` 构建参数指定本机已固定的Electron44.7.1输入，73个文件与前一独立普通副本逐一hash相同；没有修改源码、跳过任何pack/sign/runtime/payload校验，也不让最终用户下载Electron。该独立构建通过，log SHA-256 `2afb5976cb67698beed69648505f002dde87298dd1c6b7e6bf426cf4e0e40e6a`。
- 最终bundle **`desktop-e3806e6dc0b2dec80106afda`**，manifest SHA-256 **`3e2090a516837a55420086d45b2248436ee48d4350f56cbcb5557c1a1402ef5b`**；14,458 resources，含manifest共14,459files / **412,712,999 bytes**（约393.6MiB）。
- 最终installer **`Agora-Paper-Agent-0.1.2-x64.exe` / 289,499,694 bytes**（约276.1MiB），SHA-256 **`1092f7058e72e4f393c91f42f71f77436dfe009d4e50392cb0516b278008f593`**，实际 **NotSigned**。本机拟交付副本位于同批 evidence 根的 `delivery-r11/`，无push/Release发布。

r11重新执行完整11步隔离真实CLI验收全部通过，不沿用r9的耗时样本：首次start **35,061ms**、全部自有进程退出后重开 **22,560ms**、两次空闲stop **2,563/2,657ms**、立即preactivation取消 **1,909ms**、取消后stop核实 **2,466ms**；每次status/stop均校验真实owner、durable state和endpoint，API/frontend/BFF/raw rewrite全真。与r9相同，这不是正式窗口或系统冷启动/忙任务退出的性能承诺，远程模型仍未配置，late-stage真实取消及完整PDF/笔记/记忆业务闭环未在本批扩展完成。

r11最终完整smoke、原位安装字节身份、资料保留和commit收据由下节收尾记录；这些步骤完成前不将它们标为通过。后续只回填文档，不再改变冻结的生产或测试代码。


## 最终收尾（2026-10-17）

- **r11必需完整smoke通过**：本地2026-10-17 00:06:53.300–01:29:37.261，exit0；**4,275 passed / 23 deselected / 61 warnings**，pytest4,948.83秒。compile、Harness/Research/API/service/composition/architecture、deterministic AgentLoop（network_calls=0）与sources validate（true/0errors/0warnings）全部完成。最终log SHA-256 **`662762f1db05efbc3bd8404f7fdbc8ce2491373a8d04d418c4644b1423c35d7c`**。r9、r10与失败构建历史均保留；未用旧通过替r11背书。
- **最终r11 installer原位安装成功**：2026-10-17 01:30:07.175–01:30:45.840，38,665ms，exit0。安装前再次核实旧App无进程且instance STOPPED/[]，没有按名终止或强退任何用户进程。入口保持 `%LOCALAPPDATA%/Programs/Agora Paper Agent/Agora Paper Agent.exe`，实际文件版本0.1.2。
- 安装后bundle `desktop-e3806e6dc0b2dec80106afda` / manifest `3e2090a516837a55420086d45b2248436ee48d4350f56cbcb5557c1a1402ef5b` 与最终候选一致；**14,458个运行资源逐文件size/hash完全匹配，无缺失、额外或漂移**。exe SHA-256 `179dff6df3a3defbdf530bf89d0a8f5484d2c60c77df4ce92c080dc2dd378f13`，app.asar SHA-256 `34112d94bf2c640cbd031e3314afb844dacbf34acb1401155990349bd07ebfe0`。
- 升级前记录的**43个资料文件全部保留且字节hash未变**；不包括Electron缓存、日志和release代码副本，不能扩大为所有未知外部文件的备份证明。没有改动用户账号、清空资料、启动真实GUI、替用户点击原生退出确认或重启系统。
- 最终源码提交仅42个精确路径；生产/测试字节与r11 freeze一致，允许的后续差异仅本README/任务/证据文档回填。40个foreign tracked文件hash未变。7个既存安装必需输入的执行文本保持原样；LF/末尾空行规范化另有收据，并未混入其他并行实现。
- 构建发生在提交前，包内source_revision诚实记录base `1807ed6d7302f22b31b9b1eed773a60f8ea8bce4`，同时以source_digest和完整源码freeze绑定实际内容；不声称已经从未来的新commit重新编译。Git导出已证实相同生产字节，最终Git commit与artifact的对应关系保存到交付收据。尚未push或发布GitHub Release。

本请求的生命周期根因修复、精简默认运行集合、可重复源码检出、匹配gate和本机原位升级已完成。**仍未完成的验收**：真实用户窗口启动/正常关闭体感、系统冷启动、无开发工具隔离Windows断网首装/升级、late-stage真实取消/忙任务退出采样，以及完整真实PDF/笔记/记忆/合格远程模型业务闭环。因此10.5与第9节完整交付矩阵保留未完成标记，不将性能预算或全产品验收自动核销。
