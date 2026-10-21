## Context

产品需求见 `prd.md`。用户已授权按文档开发，当前处于实现与集成验收阶段；现有 `/paper-agent-lab` 为隔离设计预览，本机 Research 工具及相关真实验收仍属于 `research-local-desktop-readiness`。共享工作区有其他变更，不能把本目录的文档完成状态解释成那些变更的发布资格。

当前可复用的是 Research application services、desktop operation admission、owner/revision 工作区、阅读/报告模型及本机运行管理。缺口是独立桌面交付、阅读优先前端的正式接线，以及个人记忆的完整用户控制闭环。本设计不预设全部接口无需修改，但也不批准重建一套 backend。

## Goals / Non-Goals

**Goals:**

- 把用户选定的黑白阅读工作台变成真实、可安装的论文 App。
- 从论文来源、检索、阅读到有证据的结果和本地记忆，维持单一 owner、可信状态与可回放运行身份。
- 保留用户电脑上的数据，明确远程处理边界，并建立故障、离线、退出和恢复路径。
- 将既有基础 change 的验收与本 App 新增验收区分开，产生可追溯的 release 证据。

**Non-Goals:**

- 不扩展到通用助理、其他业务板块、系统自动化、云记忆同步或无人值守执行。
- 不用静态前端、硬编码回答、URL 快捷方式充当安装版 App。
- 不在未授权时启动收费调用、发布未验收安装构建或替用户重启电脑。用户授权开发不等于批准费用或操作系统重启。

## Decisions

### D1. 新增产品 change，不覆盖两个既有 owner

`paper-agent-app-design-lab` 保留设计预览身份；`research-local-desktop-readiness` 继续拥有运行/身份/操作/恢复基础；本变更新增宿主、正式 UI 接线与个人记忆产品要求。引用其现有合同，不新增 compatibility layer 掩盖未验收基础。

替代方案：直接把 lab 变成正式默认入口，或重写已有 readiness PRD。拒绝原因：会混淆样本与真实业务，还会覆盖其他正在进行的任务及其验收记录。

### D2. 桌面宿主是有界集成层，不是新业务后端

目标结构：

```text
Desktop host (window + narrow native bridge + lifecycle adapter)
  -> local frontend / existing same-origin BFF
    -> interface application services (identity / workspace / desktop operations)
      -> Research application services + bounded Harness
        -> ports -> approved tools / artifacts / local stores
```

窗口只加载受信本地应用来源。native bridge 仅暴露窗口动作、用户选文件/目标位置、应用诊断和管理自身实例所需接口；参数与调用来源均验证。拒绝任意 shell、任意路径读写和渲染器直接数据库访问。

本地服务的启动、重复启动、drain、停止、日志和恢复继续交给既有 runtime manager。宿主不能仅凭端口就接管进程，不能越过 instance identity 或关闭已有用户服务。

替代方案：为桌面单独写一套 Python 启停/存储逻辑。拒绝原因：双 owner 会造成端口、writer 和退出语义冲突。

### D3. 实施前用有界验证冻结宿主与安装方案

文档评审阶段未锁定框架；实现阶段当前候选采用 Electron 的窄宿主，继续承载真实 Next.js server/BFF 与既有 Python runtime。决策及尚未通过的证明见 `evidence/desktop-host-adr.md`，依赖输入见 `evidence/runtime-dependency-inventory.md`。M1 仍需验证以下决策条件，不以框架选择代替安装验收：

1. 承载现有前端路由、服务端渲染/BFF 和本地身份；不能假定当前 Next 应用可全部静态导出。
2. 启动被管理的 Python/必要前端运行依赖，无需用户开发环境或 PATH 上任意解释器。
3. 独立窗口、文件对话框、外链、快捷键、缩放、中文输入法与黑白主题可用。
4. loopback/Origin/Host/会话边界、桥接权限、single-instance/退出协议和异常恢复可验证。
5. 可追溯 Windows 构建、依赖清单、升级保留资料、签名实际状态及分发限制明确。

验证用于作技术选择，不得留下无法正式运行的演示宿主充当 v1。没有实际测量就不以体积/性能形容词作决策依据。可考虑不同宿主或系统 WebView，但只能由上述项目条件决定。

### D4. 提炼视觉组件，真实数据接线由 feature application adapter 负责

正式 UI 不导入 lab 的 `data.ts`、样本计数、问题预览或页面内存保存逻辑。只在职责清晰时提炼主题/布局/阅读组件；正式页面使用真实 domain/read model，不为方便再造形状相似的平行论文模型。

保留三栏和按需助手，落实侧栏仅 `Agora` 的品牌修订。助手与列表互斥/抽屉状态属于 UI；论文身份、文档修订、收藏、笔记、阅读位置、任务结果属于服务合同。主题可以存在本地偏好配置，但业务保存不能依赖 `localStorage` 或 React state。

替代方案：将预览的六条数据替换为 API 后保留所有原页面状态。拒绝原因：临时笔记、假对话和无 owner 的状态会继续伪装成正式业务。

### D5. 复用明确的后端入口，新增接口先补合同

以下为已核对的 Python API 路径；前端继续通过已存在的同源代理映射，不能机械假设两个层的前缀相同。

| 需求 | 现有入口 / owner | 接线注意事项 |
| --- | --- | --- |
| 检索 | `/api/v1/research/guided/intent`、`/api/v1/research/guided/search` | 展示解析条件与真实来源；本地过滤不能走在线工具 |
| 本机身份与能力 | `/api/v1/research/desktop/identity`、`/api/v1/research/desktop/capabilities` | 保留本地账号、owner、配置/已探测/已验证的区别 |
| 导入 / 准备 / 分析 / 问答 | `/api/v1/research/desktop/operations/{import,prepare,analyze,ask}` 与 request 查询 | 绑定 actor、request、语义身份与恢复代际；超时先查，不自动重发 |
| 阅读 | `/api/v1/research/desktop/papers/{paper_id}/reader` 与私有 source | 保留访问控制、材料修订与定位证据；不直接公开本地文件路径 |
| 工作区 / 笔记 | 现有 workspace/history/read-session application service | 先冻结映射、revision/CAS 与 owner，不创建第二份收藏或笔记数据库 |
| 报告 / 导出 | `/api/v1/research/desktop/reports` 及现有 export 合同 | 质量合格结果、草稿身份和可发布成果分开 |
| 个人记忆 | 先清点 Research memory port、admission、framework memory 及本地 owner | 已有 write-reading-note 不等于个人记忆完整 CRUD；缺失部分经 application service/port 补齐 |

API 缺口在实现前形成 request/response/error 与权限测试。interface 层不绕过 application service 直达 executor 或 store。

### D6. 将个人记忆与笔记、证据、系统修复记忆分开

新增个人记忆合同前先确认现有 ownership。最终至少支持 owner-scoped record、revision、来源、类别和候选/启用/停用/删除状态；现有阅读笔记或 repair memory 不直接重命名成用户偏好。

候选由模型提出或用户手动输入；应用层进行确定性授权、校验及用户确认后才写入。活动存储和派生检索索引都在本机；停用、更新和删除要在后续召回中一致生效。索引重建不得复活删除条目。记忆来源丢失、导出副本和历史对话保留都有明确说明。

按次召回记录具体 memory ID/revision 与用户允许范围。当前指令覆盖历史偏好，未确认候选不得召回；用户关闭本次记忆后，构建上下文和远程调用都不能带入这些片段。已经发送的外部请求不可假称已撤回。

替代方案：保存所有聊天文本，并称其为“自动记住你”。拒绝原因：违反用户控制、来源质量和本机数据边界。

### D7. 网络、工具与 Harness 维持既有安全边界

本机落盘与外部处理分别建模。搜索、模型、embedding 记录目的地和被授权的数据范围，未获许可的个人记忆不得因索引或摘要路径外发。API key 不进入渲染器、默认日志或导出。

保持 `source collection -> evidence -> agent analysis -> report -> quality gate -> artifacts/storage`。Harness 有界 `PLAN -> EXECUTE -> VERIFY`、确定性 gate、turn/replan/retry budget 与 durable transitions 不因 desktop 模式改变。LLM 不决定权限、记忆写入、路由、发布或 skill promotion；普通函数能做的 UI、排序、持久化确认不交给 agent。

外部论文、工具返回及文档内指令均为不可信内容。发现“执行电脑操作”等文字不能扩大工具集合或宿主桥接权限。

### D8. 安装、升级、恢复沿用数据 owner，不混入演示状态

App 用户资料与安装资源分开。已有本机 Research 数据只在确认 owner、schema 和备份后接管/迁移，不移动未知绝对路径、不扫描整盘；lab 页面临时样本不迁移成真实资料。

新增记忆 owner 必须加入既有备份 inventory、schema/release 兼容与隔离恢复验证。恢复前清楚说明旧记忆与 pending operation，使用恢复代际拒绝旧请求自动重放；旧版本不兼容时拒绝降级。卸载默认保留用户数据。

替代方案：直接复制 `.newsroom` 或把整个开发仓库打进安装资源。拒绝原因：会带入 secrets、缓存、绝对路径和未经验证的 writer/owner 状态。

### D9. 设置变更由运行配置 owner 管理，不从渲染器直接修改文件

F-08 必须提供模型与论文检索配置的真实管理闭环；能力状态、数据目录和诊断只读展示不能替代配置编辑。该能力正在实现，尚不作为已通过证据。

- 普通页面仅编辑非秘密的模型路由、模型名、受校验的服务地址及受支持的论文来源参数；不接受任意环境变量名、凭据值、文件路径或新闻/项目来源。
- 唯一运行配置 owner 负责版本、revision/CAS、校验预览、单次确认票据与原子写入。预览不写入配置、不启动模型、不探测收费接口。
- Windows 凭据存放在系统 Credential Manager 的本 App / 数据根专属槽位，使用原生录入入口；普通渲染器只取得是否存在的状态，密钥不进入 JSON 协议、配置文件、release manifest、诊断或默认备份。
- 显式应用前重新核对未保存编辑和活动任务，再由既有生命周期 owner 安全停止本 App 进程。取消、CAS 冲突、漂移或校验失败不得伪报成功，也不得无授权接管其他服务。
- 配置身份由 ReleaseManager 公共接口重新验证。变更产生新的不可变配置绑定候选，保留源代码与依赖身份；不能原地篡改 active release 或接受渲染器提交的 digest。应用失败保留可核对的旧配置 / 状态，不以部分重启冒充成功。
- API composition、Research worker 和 guided intent 使用同一受控配置投影。缺少密钥仍可打开本地资料与管理记忆；“已配置”不等于“provider 已合格验证”，真实收费检索、分析或问答仍须用户显式提交。

### D10. 清除全 profile 由一次性授权和退出后唯一执行者控制

PRD 的独立“删除全部数据”不能用恢复覆盖、卸载或手工删除测试 profile 代替。设置先展示真实作用域及停止服务的预览副作用，精确中文输入与默认取消的原生确认是两层独立授权；未保存草稿、活动任务或未知 lifecycle 必须受到保护。

- Renderer 只接收非敏感预览 ID/到期时间/范围与真实状态，不接收路径参数、私有 capability、密钥或 shell。主进程绑定当前实际 userData，Python 绑定其固定 runtime 子根及 marker、目录文件身份、代际、范围摘要、release 与一次性票据；预览不能静默收编未知旧目录。
- 沿用唯一 lifecycle owner 安全停止服务。私有 apply/finalizer 授权通过有界 stdin 传递，不进入 renderer、日志或命令行。准备阶段仅报告 PREPARED，不提前声称删除成功。
- 安装资源中的固定 finalizer 等待精确宿主 PID 与 OS 创建身份退出后，才作为唯一文件删除执行者清理整个已授权 userData 和该 profile 专属 vault 槽位；不按端口/名称杀进程，不触及外部备份、导出、导入原件、安装资源或其他 profile。
- 维护 journal 位于目标外的固定受保护位置，仅含最小操作身份摘要和状态，范围预览必须披露。pending、删除部分失败或凭据状态未知时阻止普通启动，绝不自动继续删除或创建空资料假装恢复；需要新的明确授权才能重试。
- root 置换、过期/replay、scope/CAS 变化、reparse/alias、停止失败或文件锁定都必须有确定性的失败测试。最终实际验收还包括真实原生确认、退出后 CLEARED、正常重开首次 bootstrap，以及外部文件未改动；只测 helper 或收到 PREPARED 不足以核销 task 7.3。
### D11. Windows 安装、业务持久化与恢复必须支持真实长路径

实际 NSIS exit0 但缺4个ARM PE与5个深层license，以及较长用户目录下真实PDF的run intent原子临时文件写入失败，都是交付阻断，不能通过删资源、改manifest、缩短测试目录或修改系统LongPathsEnabled绕过。

- 保持锁定的标准electron-builder/NSIS安装、升级和uninstaller路径。项目使用官方include/customHeader与经实际编译证明的窄shim替换解压宏；生成文件只include经完整hash固定的上游模板并重定义目标宏，不fork整个builder、不修改node_modules、不接管整个installer脚本。模板顺序、版本或digest漂移必须阻断构建。
- 内嵌固定digest的现代decoder、已验证stage Node和确定性bootstrap。执行任何内嵌程序前，由独立NSIS hash能力核对工具/脚本/manifest/archive；只用固定绝对路径与固定argv，移除可变Node/工具环境，不从PATH、用户配置或网络选择执行程序。decoder许可证完整随包保留。
- 安装归属准入须发生在旧版本卸载或覆盖pre-file之前，解压入口再次核对最终目标；新空目录和本次固定pre-file可被严格识别，未知非空目录、路径别名、reparse或身份变化不能被自动接管。归属marker只是证据之一，不能单独授权删除任意目录。
- 现代decoder直接解压至获准安装树，不再经过丢长路径的旧decoder或CopyFiles。以有界总时限执行与核验：完整Electron payload和runtime资源集合、每项size/full SHA、目录/链接类型及extra/stale文件均必须通过，才允许标准注册、快捷方式或启动。exit0、文件数量相同或抽样9项不能替代完整gate。
- 默认卸载与真实升级必须处理modern decoder创建的长路径安装树，且默认保留独立userData。若标准删除无法完成，使用官方窄customRemoveFiles hook与固定、受验证且归属受控的执行者修复；不删除profile、不按名称杀进程、不把未知残留报告为成功。升级失败须披露旧二进制和新安装的真实状态，不承诺未实现的原子rollback。
- 业务JSON锁、完整UUID原子临时文件、fsync/replace和备份snapshot/archive/isolated restore使用内部extended-length I/O路径，公共业务身份、路径字段、引用和manifest仍为正常规范路径。文件锁、失败清理、源文件字节/WAL不变、owner隔离与checksum规则不得弱化；同样不能只修安装解压而留下首次导入或恢复失败。
- 验收覆盖LongPathsEnabled=0的真实长目录、ARM过滤器、完整license、同大小错误字节/缺项/extra、环境投毒、超时、锁定/权限/磁盘失败、实际卸载和升级的数据保留，以及正常默认profile的真实App闭环。用户同权限恶意竞态或内核永久挂起不被描述为已建立安全隔离/绝对wall-clock保证；检测到未知归属或无法核验时失败封闭并保留证据。

## Risks / Trade-offs

- [现有后端有入口但真实 provider/材料尚未通过验收] → capabilities 如实降级，先补合格配置与真实闭环，不把待配置作为发布通过。
- [与 active readiness 变更并行造成两套生命周期/数据归属] → 明确 owner、复用管理器、冻结输入后验收，不覆盖其未完成任务。
- [桌面被当作可信环境而扩大渲染器权限] → 窄桥接、来源/参数校验、会话与 owner 测试、远程导航隔离。
- [用户将“本地记忆”误解为绝不联网] → 将本地持久化和推理/embedding 外发分开说明与控制。
- [记忆删除但旧索引仍可检索] → 带版本的失效与 tombstone、重建/重启回归、恢复旧备份再次确认。
- [大包、启动成本或可选解析依赖影响安装] → 实际宿主验证与依赖清单，按实测记录限制，不先承诺性能。
- [共享工作区 gate 失败或输入漂移] → 仅提交本变更文件；冻结候选输入后重跑适配检查与完整 smoke，不放宽断言。

## Migration Plan

1. 评审 PRD 与首发假设，确认基础变更未完成项及责任。
2. 用 ADR 确定宿主/依赖/安装策略，验证认证与运行管理边界。
3. 在真实 owner 上逐条接入资料库、阅读、问答和笔记；既有入口在默认入口切换前保持可用。
4. 接入个人记忆合同、用户控制、外发策略和 backup inventory；验证现有数据及隔离恢复。
5. 绑定同一源码/release/config/dependency 完成 AC-01–AC-16 和项目 gate，再发布 App；不提前下线独立 lab 预览。
6. 回退仅切换验证兼容的 release；不兼容需显式配套备份恢复，不进行破坏性逆迁移或回滚用户开发工作区。

## Open Questions

- 首发是否确认 Windows x64，最终安装名称与签名/分发渠道是什么？当前按 PRD 假设规划，不阻塞文档。
- 真实检索/模型验收可用哪些 provider 与预算？未确认前不发起付费验收。
- M1 的宿主与依赖打包 ADR、个人记忆 owner/API 缺口需要完成技术核对后冻结；不改变论文专用、本机记忆和用户控制的已确认原则。
