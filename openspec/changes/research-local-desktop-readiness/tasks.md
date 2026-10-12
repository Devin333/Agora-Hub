# Research 本机日常使用版实施任务

当前状态：Proposed。所有复选框表示后续交付任务；创建本清单不代表功能已经实现或测试通过。

执行顺序：**先完成第 1–10 章的全部生产代码、迁移/运行工具及必要测试代码，再执行第 11 章集中验证，之后执行第 12 章真实验收与本机部署。** 实现阶段允许静态代码阅读、合同审查、diff 与规格校验；不提前运行测试套件、typecheck、lint、build、compile 或 smoke。测试编写随实现完成，运行结果单独留在 11/12 章。

既有实现先核对并复用，只修补本合同所需缺口，不重复建设已完成能力。代码任务勾选仅表示实现完成；整份交付仍须满足最终验证和真实使用证据。完整产品范围、成功标准和 AC-01–AC-17 见 [prd.md](prd.md)，技术归属和迁移策略见 [design.md](design.md)。

## 1. P0：冻结本机使用合同与现有能力映射

产物：baseline/接口/owner/存储清单，后续实现可以直接据此接线。依赖：本 proposal、PRD、specs。

- [ ] 1.1 记录实际 Git/source identity、工作树状态、Windows/Node/项目 Python 身份和现有前端 lockfile；只读取环境配置存在性，不收集秘密值。
- [ ] 1.2 清点正式/旧 Research 首页、论文列表、两类 reader、分组、报告、登录和 BFF 路由，形成迁移表与返回/续读参数合同。
- [ ] 1.3 逐项核对既有 workspace、guest import、notebook、selection、report draft、reader progress 的 owner、API 和持久化位置，区分已实现接线与真实缺口。
- [ ] 1.4 追踪 parse/analyze/ask/rag-ask/analysis/reader/trace 的 application 调用、权限、run/result/artifact DTO、同步/异步边界和当前恢复能力。
- [ ] 1.5 清点真实数据根、配置覆盖、JSON/SQLite/index/大文件及其锁、一致性与引用关系，建立后续备份使用的 storage inventory。
- [ ] 1.6 固定必需/可选 capability 表：至少真实来源、PDF 导入、单论文分析/问答、报告与持久化；标明 Docker/OCR/dense retrieval 的条件依赖。
- [ ] 1.7 固定请求、错误、来源修订、报告状态与 actor 合同，明确哪层拥有权威；需要补充的字段只在原 application/port 边界加入。
- [ ] 1.8 建立本 change 与 `research-workspace-efficiency` 5.2/5.3 的证据映射，以及其他 active change 不继承发布资格/不自动启用实验的清单。

## 2. P0：本机 profile、配置和能力诊断实现

主要落点：现有 environment/composition、frontend config、local desktop 配置模块。依赖：1；对应 D2、AC-01/17。

- [ ] 2.1 实现 `local-desktop` 严格枚举 profile 和单一配置解析，明确 CLI/运行配置/根环境/默认值的覆盖顺序与未知字段拒绝。
- [ ] 2.2 将 frontend/API 的 bind、port、canonical origin、BFF API base 和 cwd 解析为一致配置，拒绝非 loopback、冲突 origin 和不安全路径。
- [ ] 2.3 固定项目 `.venv` 解释器解析，记录 Node/npm/lockfile 与 Python 实际依赖身份；缺失时提供准确修复方法，不回落到系统 Python。
- [ ] 2.4 实现配置、读写目录、可用磁盘、端口与 frontend build 的本地只读检查，并输出稳定 reason code 和 JSON。
- [ ] 2.5 经 application service 构建 source/parser/LLM/ask/artifact capability 投影，分别记录 configured/reachable/verified 和验证时间，禁止用单一布尔值误报。
- [ ] 2.6 实现显式网络/provider probe，复用真实 adapter，约束目标、超时、请求数、输入/输出与费用预算；默认 doctor 不联网调用模型。
- [ ] 2.7 根据所选解析路径检查 Docker engine、image/model 与能力要求；依赖缺失只影响对应能力，不伪造 OCR/解析成功。
- [ ] 2.8 接入前端配置/能力提示，区分可用、缺配置、网络不可达和未验证，并给出本机可操作的恢复说明。
- [ ] 2.9 补充脱敏本机配置样例及读取说明，防止 provider/server token 进入 `NEXT_PUBLIC_*`、CLI 参数、release 或诊断输出。
- [ ] 2.10 编写配置优先级、loopback/origin、解释器选择、缺依赖、probe 预算及秘密脱敏的合同测试代码，留到第 11 章执行。

## 3. P0：本地账号、会话和服务端身份贯通

主要落点：frontend auth/BFF、API auth、Research actor/application service。依赖：1/2；对应 D3、AC-02/04/17。

- [ ] 3.1 在本机 profile 正式开启前端登录保护，连接首次初始化、登录、session 查询、到期、退出与安全 next path。
- [ ] 3.2 核对并补齐 first-user bootstrap 的原子性与重复请求行为，不覆盖已有账号，不创建默认口令或演示身份。
- [ ] 3.3 统一 cookie profile helper；仅对确定的 loopback HTTP 放宽 Secure，保留 HttpOnly/SameSite/有效期/撤销和非本机 HTTPS 要求。
- [ ] 3.4 补齐可信 session 到 Research actor scope 的服务端绑定；服务 API token 与用户身份分开，拒绝浏览器伪造 user/tenant/namespace。
- [ ] 3.5 在 workspace、imports、notebook、run、private reader/artifact 和 report/export application 边界统一 owner 验证，保留原有明确公开资源合同。
- [ ] 3.6 检查所有 BFF、`/api/v1` rewrite 和直接 API 路径，阻止绕过页面 guard 的私有读写；业务写操作要求会话/同源，bootstrap/login 只保留已校验 origin/输入/限流/初始化状态的匿名认证入口例外。
- [ ] 3.7 完成退出/过期/账号切换后的缓存清理、请求失效和原 owner 草稿恢复，避免异步旧响应污染新账号。
- [ ] 3.8 将本地用户名密码入口接入正式工作区；公共 OTP/OAuth 未配置时诚实展示，不作为本机登录前置条件。
- [ ] 3.9 编写 bootstrap 并发、cookie profile、session 到期/撤销、越权 API、伪造 scope、跨 origin、账号切换及共享 token 不提权测试代码。

## 4. P0：正式入口、路由迁移和桌面交互完善

主要落点：frontend app、portal/research/reader components、navigation。依赖：1/2/3；对应 D4、AC-03/16。

- [ ] 4.1 将现有研究首页抽取为正式共用组件，在本机 profile 的 `/` 接入；保留其他 profile 和非 Research 模块的既有入口语义。
- [ ] 4.2 将论文列表、详情/阅读、分组和报告统一到设计中的正式路径，核对 taxonomy、返回链接和当前选中导航。
- [ ] 4.3 为既定 `/design-demo` 研究深链接实现限定迁移 redirect，保留合法 query/定位并拒绝外部 returnTo；不维护第二份业务页面。
- [ ] 4.4 更新所有新建历史、草稿、来源卡片、命令面板和模块跳转的 URL 生成器，统一路径校验规则。
- [ ] 4.5 实现旧历史/草稿 URL 的幂等规范化和 owner-scoped CAS 写入，失败保留原记录和可重试提示。
- [ ] 4.6 完成首页、侧栏、分组、论文、研究结果和报告的导航连续性，避免跳转丢失当前问题、选择材料和目标分组。
- [ ] 4.7 补齐关键页面的 loading/empty/error/retry/expired/unavailable 状态，清理对用户无意义的 demo 标签和技术占位文案。
- [ ] 4.8 修整 1366×768、1920×1080 与 125% 缩放下的输入区、侧栏、reader、弹窗和滚动区域，保证关键操作不被遮挡。
- [ ] 4.9 补齐键盘可操作性、焦点恢复、表单 label、异步状态和错误提示，沿用现有设计语言。
- [ ] 4.10 编写正式/旧路由、query/定位保留、幂等迁移、导航状态和关键键盘/桌面布局测试代码。

## 5. P0：工作区、笔记、材料与精确续读

主要落点：workspace/history、reader/notebook services 和对应 BFF。依赖：1/3/4；对应 D5、AC-04/07。

- [ ] 5.1 核对并补齐分组/材料 CRUD、移组、归档/恢复和引用约束，使现有前端操作均进入真实 owned application service。
- [ ] 5.2 统一保存状态与服务端回执，区分 dirty/pending/synced/conflict/error，禁止仅 localStorage 写入就显示账号已保存。
- [ ] 5.3 完成双标签页 revision/CAS 冲突的输入保留、重新读取和显式重试，禁止静默覆盖其他标签页更新。
- [ ] 5.4 补齐 guest/legacy 内容显式导入、去重和失败恢复，不在登录或账号切换时自动合并资料。
- [ ] 5.5 将本版需要长期保留的 notebook/selection 接到其已有 application owner，处理 legacy-local 导入与权限，不另建笔记数据库。
- [ ] 5.6 持久化章节、PDF 页、source revision、报告草稿位置和会话恢复引用，保证已提交活动不只存在于客户端内存。
- [ ] 5.7 对来源修订变化、定位失效、材料删除和缺失引用给出可读提示与安全恢复位置，不伪装精确续读成功。
- [ ] 5.8 贯通选中材料复用、确定性比较和报告准备，保留真实来源、用户笔记与目标分组，不生成假模型结论。
- [ ] 5.9 处理网络断开、磁盘/存储失败和账号切换期间的未同步内容，确保重试不会越 owner 写入。
- [ ] 5.10 编写 CAS 并发、owner 切换、guest/legacy 幂等导入、笔记持久化、修订失效和重建 service 后续读测试代码。

## 6. P0：真实搜索、来源输入与 PDF 导入

主要落点：Research guided/source/import application 与 frontend source UI。依赖：2/3/4/5；对应 D6、AC-05/06。

- [ ] 6.1 将研究问题、可编辑约束、来源选择和目标分组绑定到已有真实搜索请求，保留失败后的原始条件。
- [ ] 6.2 完成 arXiv/DOI/GitHub 引用识别及对应读取/搜索/材料保存入口，不把 GitHub 来源转换成代码执行。
- [ ] 6.3 补齐授权 PDF 上传的大小/类型、文件名及路径校验，沿用已有 source policy、私有存储和解析隔离。
- [ ] 6.4 贯通 import 创建、状态查询、retry 与 reader 跳转；首次发送前保存 owner-scoped request/代际，服务端绑定 import/run/source 并支持首次回执丢失后的查询，处理重复点击。
- [ ] 6.5 展示真实上传/解析/发布状态；区分 metadata-only、可阅读、来源拒绝、解析失败与组件不可用，不模拟进度。
- [ ] 6.6 限定轮询/等待/网络请求预算，离开页面后释放前端资源；恢复页面先查询既有导入，不重复创建。
- [ ] 6.7 确保 reader 只消费已获资格且可校验的正文/资产，来源/权限/完整性错误保持可诊断，不使用替代正文。
- [ ] 6.8 将成功导入和来源选择与工作区材料、历史、分组及精确阅读状态关联，保留 owner 和来源版本。
- [ ] 6.9 编写 source adapter 边界、上传限额/权限、导入去重、失败重试、metadata-only 与 reader 资格负例测试代码。

## 7. P0：单论文分析、问答与可恢复请求

主要落点：Research application/ports、API/BFF 和运行反馈 UI。依赖：1/2/3/6；对应 D7、AC-08/09/10。

- [ ] 7.1 冻结前端分析/问答 DTO 与现有 ResearchAnalyze/Ask 输入映射，显式选择论文、来源修订、问题及证据范围。
- [ ] 7.2 实现服务端 options allowlist 与既有预算限制，拒绝用户控制 gate、routing、工具授权、实验开关和 publication。
- [ ] 7.3 在既有 application/persistence port 实现 owner-scoped request/run 身份与 fingerprint；首次发送前保存 request/代际/语义，服务端原子 durable admission 后才执行，并支持按 request 查询，不在 BFF 建新 job store。
- [ ] 7.4 实现并发相同请求复用、不同内容标识冲突和已完成结果查询，确保服务端去重不依赖 UI disabled。
- [ ] 7.5 建立缺失的 scoped 状态/结果/诊断查询投影，复用现有 run record、transcript、event 和 artifact 的权威数据。
- [ ] 7.6 将正式工作区和 reader 的分析入口接入既有 Harness-controlled runtime，保留受控 PLAN/EXECUTE/VERIFY 与 gate failure 行为。
- [ ] 7.7 接入真实单论文问答和证据回跳；可选 rag-ask 受实际能力限制，不以 fake 或未准入检索替代。
- [ ] 7.8 完成等待、运行、需操作、通过、拒绝、失败、中断和待核对的真实状态 UI；缺事件时只显示等待。
- [ ] 7.9 实现刷新/断连/响应丢失后的请求身份恢复与先查询后重试，避免重复付费执行和错误最新结果关联。
- [ ] 7.10 区分停止浏览器等待和后端取消；仅在既有取消服务确认后显示已取消，不存在能力时不提供假取消。
- [ ] 7.11 将当前 run 与工作区会话/所选材料关联，同时保持 UI、launcher 和 LLM 不拥有 gate/发布/记忆写入权。
- [ ] 7.12 编写真实 application 路径的并发 admission、请求冲突、首次回执完全丢失后刷新、超时/崩溃恢复、scope/option 拒绝、gate 失败和无盲重试测试代码。

## 8. P0：Research 报告投影、引用、保存与导出

主要落点：Research application projection、frontend reports、owned workspace refs。依赖：5/7；对应 D8、AC-08/10/11。

- [ ] 8.1 定义 Research 报告 DTO，映射既有 analysis/quality/run/source revision/artifact/citation，不将通用 reports 模型强塞进 Research store。
- [ ] 8.2 实现 authenticated report projection 和引用解析，在 application service 校验 owner、完整性和质量资格，不直读 store 绕过边界。
- [ ] 8.3 构建真实结果阅读页面：摘要、分析内容、适用条件、质量状态、来源和诊断，缺失信息明确显示。
- [ ] 8.4 将 citation 链接回对应论文、来源版本、章节/页码/其他已支持 locator；过期、损坏或无权限时可诊断。
- [ ] 8.5 实现报告列表/保存/重新打开所需的 owned 结果引用与草稿关联，正文和质量仍以原 run/artifact 为准。
- [ ] 8.6 明确区分多材料草稿、确定性比较、模型候选、失败结果和已接受单论文报告，不将草稿标为自动综述。
- [ ] 8.7 实现受控 Markdown/JSON 导出并保留来源、条件、run/revision、quality 和草稿标记；拒绝越权与失败候选的 accepted 导出。
- [ ] 8.8 补齐 Markdown/链接/文件名安全处理、导出下载反馈和中断后重试，不泄漏内部路径或上游响应。
- [ ] 8.9 编写报告资格/owner、源结果一致性、citation 修订、损坏 artifact、草稿标识与安全导出的测试代码。

## 9. P1：Windows 本机启动、运行和日志工具

主要落点：`scripts/local-desktop.ps1`、`scripts/local_desktop` 模块、release/config adapters。依赖：2 及 3/6/7 的实际接口；对应 D9、AC-01/12/17。

- [ ] 9.1 实现薄 PowerShell 入口和 Python CLI 分发，固定项目解释器/cwd、参数校验、帮助、稳定退出码及 JSON 输出。
- [ ] 9.2 接入 doctor/能力结果并定义运行记录 schema：instance/release/config identity、端口、PID、创建时间、child/container 所有权及日志引用。
- [ ] 9.3 实现单实例锁与原子操作记录，处理重复 start、过期记录、PID 复用及非自有端口冲突。
- [ ] 9.4 实现 build/release manifest 与完整性校验，包含前端生产产物、后端源码/资源、lock/resolved dependencies 和配置 schema，排除真实数据与秘密。
- [ ] 9.5 实现固定 release 路径的后端与前端后台启动，安全传参、隐藏窗口、固定环境，并仅使用预先准备的依赖/模型。
- [ ] 9.6 实现 API/BFF/必要能力的有界 readiness，区分 READY/DEGRADED/FAILED；启动部分失败只收敛本次自有资源。
- [ ] 9.7 实现 stop/restart 的停止 admission、等待当前有界工作、busy 反馈和所属进程检查，不删除业务记录或伪造任务完成。
- [ ] 9.8 实现 status、脱敏日志写入/轮换/有界读取和异常退出记录；若支持自动恢复，加入明确重试上限与退避。
- [ ] 9.9 实现 open、桌面入口及显式 autostart enable/disable；自启默认关闭，仅管理当前用户下本实例的启动项。
- [ ] 9.10 对选定 Docker parser 使用已知容器/镜像身份和 loopback 限制，不启动无关组件或自动下载大模型。
- [ ] 9.11 固定 release 激活与代码/依赖身份验证，避免运行可变工作树或将任意旧 `.next` 视为当前构建。
- [ ] 9.12 编写 CLI/config/锁/进程归属、端口冲突、部分启动失败、readiness deadline、停止 busy、脱敏日志和自启开关测试代码。

## 10. P1：一致性备份、隔离恢复与版本恢复

主要落点：本机工具的数据 adapter，复用各 store 的现有一致性边界。依赖：1/5/7/8/9；对应 D10、AC-13/14。

- [ ] 10.1 将 storage inventory 转为受校验的备份计划，覆盖实际自定义数据根、必需 owner、schema、引用和恢复方式，明确缓存/模型/release 排除项。
- [ ] 10.2 实现停止 admission、收敛 writer 和获取原 owner 一致性锁的备份准备，发现未知 writer 或未支持存储时拒绝。
- [ ] 10.3 实现 JSON/大文件和 SQLite 一致快照处理，正确覆盖 WAL/sidecar 状态；复制失败不改变源数据或既有有效备份。
- [ ] 10.4 定义 backup manifest 与 bounded 打包：规范化相对路径、类型/大小/checksum、build/schema、owner 及外置配置要求。
- [ ] 10.5 对备份和恢复目录应用合适的当前用户访问控制，排除原始 `.env`/provider 密钥/敏感内容日志；恢复时撤销已有会话。
- [ ] 10.6 实现 staging restore 的文件数/大小限制、path containment 和 symlink/junction/绝对路径拒绝，不写出受控目录。
- [ ] 10.7 在隔离目标校验完整性、schema、owner、run/artifact 引用及必要索引；将各 owner 配置映射至恢复根，拒绝不安全重定位，在原业务根对校验进程不可读时仍须读取真实恢复资料，报告通过后才允许激活。
- [ ] 10.8 实现停服务的多 owner 激活 journal、预恢复快照和中断恢复，失败保持原一致状态，清理只触及验证过的自有路径。
- [ ] 10.9 实现 rollback 的目标 release/依赖/配置/data schema 兼容性校验，兼容时只切换代码版本，不覆盖业务数据。
- [ ] 10.10 对不兼容旧版本拒绝原地降级，支持明确指定经验证兼容备份的恢复流程，不做自动破坏性逆迁移或 Git 工作树回滚。
- [ ] 10.11 在既有 admission/auth 控制元数据实现恢复代际或等价 fencing，拒绝旧请求自动执行和恢复 intent 自动重放，not-found 不授予重试许可，显式新执行使用新身份。
- [ ] 10.12 编写自定义数据根、并发 writer、SQLite/WAL、磁盘失败、checksum/引用错误、路径逃逸、会话/旧请求失效、隔离恢复不读原根、半途激活和兼容性负例测试代码。

## 11. P0：全部代码完成后的集中检查与修复

准入：第 1–10 章的生产代码与测试代码全部完成，经静态合同审查；禁止用下列检查取代未实现功能。对应 D11、AC-01/02/04/09/10/12/13/14/16/17 的自动证据。

- [ ] 11.1 审查第 1–10 章 diff、依赖方向、所有权、无 placeholder/fake production 和全部规格覆盖；冻结候选源码/依赖/配置身份。
- [ ] 11.2 在 `frontend` 执行全量 `npm run test`、`npm run typecheck`、`npm run lint` 和 `npm run build`，记录命令、结果与日志。
- [ ] 11.3 使用项目 `.venv` 执行 `python -m scripts.dev compile` 和 `python -m scripts.dev test`，确保共享 API/Research/Harness 路径的全量回归结果明确。
- [ ] 11.4 执行必需 `python -m scripts.dev smoke`，保留架构、Research/API/service、AgentLoop offline smoke 与 sources validation 结果。
- [ ] 11.5 执行新增 Windows lifecycle/backup/recovery、auth/owner/request admission 合同测试和前端 browser 回归；区分自动 fixture 证据与真实服务证据。
- [ ] 11.6 执行 `openspec validate research-local-desktop-readiness --strict`，并严格校验本次实际修改到的其他 active change，审计任务/规格映射。
- [ ] 11.7 对所有失败修复根因，重跑受影响检查；若最终运行代码/共享合同改变，保证最终 full test/smoke 与构建证据覆盖新候选。
- [ ] 11.8 核对最终代码、Python/Node/依赖、配置和构建身份一致，记录真实跳过项及理由，不将环境缺失转换成通过。
- [ ] 11.9 审计秘密、授权入口、loopback 绑定、受控删除/移动范围与文档链接，生成脱敏集中验证记录并在检查通过后提交代码。

## 12. P0：真实浏览器验收、本机部署与交付

准入：11 已通过，使用同一候选构建与准备好的真实配置。对应 D12 和全部 AC；不得用 route mock/fixture 替代真实成功流程，不自动重启用户电脑。

- [ ] 12.1 准备有界真实验收计划：来源、PDF、模型/profile、调用/费用预算、隔离数据位置及回收范围；创建现有真实资料的有效备份。
- [ ] 12.2 在 Windows 启动候选 production release，执行 doctor/readiness，核实实际 API/BFF、loopback、构建和依赖身份。
- [ ] 12.3 用真实本地账号完成初始化（仅未初始化时）、登录/刷新/退出/再登录；核对既有账号不被覆盖和私有 API 无绕过。
- [ ] 12.4 用真实浏览器完成正式入口/旧链接、搜索与 arXiv/DOI/GitHub 引用流程，记录真实来源、错误恢复及关键 request 身份。
- [ ] 12.5 导入至少一份真实 PDF，验证解析/发布后阅读、笔记、分组、材料复用和精确续读；保留 import/run/source/artifact 证据。
- [ ] 12.6 使用真实 provider 完成单论文分析与问答，核对质量结果、引用定位、报告保存、Markdown/JSON 导出与再次打开。
- [ ] 12.7 演练双标签冲突、会话变化、网络/响应中断后的输入保留和既有运行核对；确认不重复执行、不伪报取消或通过。
- [ ] 12.8 在实际 Windows 环境验证重复 start、端口占用、异常退出、stop/restart、日志和资源清理，不影响无关进程。
- [ ] 12.9 对真实资料执行备份与隔离恢复演练，在原业务根对校验进程不可读时重新登录核对材料/笔记/论文/报告；验证旧请求不自动重放，保留 manifest/checksum/恢复代际和结果。
- [ ] 12.10 用两个明确 release 身份完成兼容版本恢复与不兼容拒绝演练，核对依赖可用和资料保留，再回到目标候选版本。
- [ ] 12.11 完成两种桌面分辨率、125% 缩放、键盘和真实加载/空白/失败状态验收，修复影响主流程的问题后补齐受影响验证。
- [ ] 12.12 在用户实际进行电脑重启后，验证从入口重新启动/登录和继续既有研究；若该操作未发生，AC-15 与本项保持未完成，不能用服务重启替代。
- [ ] 12.13 部署最终已验收构建并提供桌面入口、中文本机使用文档、配置/诊断/停止/日志/备份/恢复说明和实际能力限制；可选自启仅在明确启用后设置。
- [ ] 12.14 将 AC-01–AC-17 证据绑定最终源码/release/config/dependency 与时间，逐项核销本清单及原 workspace 5.2/5.3 的实际满足部分；保持其他 change 资格与实验开关原所有权，审计并提交最终交付记录。

任务关联：

| Capability | 主要实现章节 | 验证章节 |
| --- | --- | --- |
| `research-desktop-workspace` | 3、4、5、6 | 11、12.3–12.5、12.7、12.11–12.12 |
| `research-desktop-run-report-flow` | 3、7、8 | 11、12.6–12.7 |
| `research-local-runtime-management` | 2、9 | 11、12.2、12.8、12.12–12.13 |
| `research-local-data-recovery` | 1.5、10 | 11、12.9–12.10 |
| `research-desktop-release-acceptance` | 1.8、11、12 | 最终 AC 与候选身份核对 |
