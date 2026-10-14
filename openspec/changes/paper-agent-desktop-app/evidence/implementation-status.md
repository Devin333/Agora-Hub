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
