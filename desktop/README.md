# Agora Paper Agent 桌面宿主

> 当前为实现和验收中的候选，不是已发布安装版。界面截图、源码测试、portable 启动和 builder 目录检查不能替代真实安装、合格模型闭环或最终项目 gate。当前验收矩阵位于 `openspec/changes/paper-agent-desktop-app/evidence/acceptance-gap-audit-20261015.md`。

## 产品与数据边界

- 本 App 仅处理论文：检索、导入、阅读、引用问答、笔记、报告和本机个人记忆。不包含新闻/项目、邮件、日历或通用电脑操作代理。
- 正式页面为 `/paper-agent`，不从 lab 复制六篇样本或演示状态。侧栏品牌仅为 `Agora`。
- 安装资源与资料分离。资料由 Electron 的实际 `userData` 管理；其 `runtime` 子目录包含本机账号/会话、论文材料、笔记/成果、个人记忆、运行配置与必要运行状态。设置/诊断显示的实际路径为准，不通过安装目录或开发仓库猜测资料位置。
- 用户原始导入文件、用户明确保存到资料根之外的导出/备份，不因清除 App 数据而删除。App 管理的导入副本属于资料根。
- 不编辑已验证的 release、资源清单或运行依赖。不要手动建空索引目录来让备份检查通过。

## 首次使用与本地功能

1. 正常启动后创建本机账号，或登录已有账号。未登录时不会展示其他 owner 的私有论文库。
2. 资料库初始为空。选择本机 PDF 后，分别核对导入回执、解析结果与资料库保存状态；重复保存应复用已识别论文，而不是增加重复收藏。
3. 未提供作者/日期时明确显示未知信息，不猜测元数据。全文、摘要和部分材料应按实际证据范围标识。
4. 原文的文本/PDF 模式、页码和笔记与材料修订绑定。界面显示已保存之前，应等待真实写入回执；冲突/失败时保留草稿，不以重新导入绕过。
5. 本地筛选不应联网。缺少合格远程配置仍可使用本地能力；“待配置”或 DEGRADED 不是远程模型已验收。

## 模型、检索与费用

- 设置中读取真实配置，编辑非秘密字段后先预览，再明确应用。应用会重新核对草稿、任务、配置 revision 和运行身份，并安全停止/重启本 App 的服务。
- 密钥仅使用系统原生录入及本 App/profile 专属槽位；普通页面、默认导出、备份和安全诊断不应取得密钥值。
- 保存模型地址/名称或密钥，不等于 provider 可用、合格或已经得到费用授权。在线检索、分析和问答应由用户明确发送；中断或重开不得自动重放可能产生费用的请求。
- 当前发布验收仍需用户确认 provider、论文来源和预算，并完成至少一次真实检索、真实 PDF、引用问答、笔记及记忆闭环。

## 个人记忆

- 普通笔记不会自动成为个人记忆。候选必须由用户确认，未确认候选不参与召回。
- 可以编辑、停用、删除和导出记忆；界面应披露实际使用的记忆 ID/revision。当前指令和“本次不用记忆”优先于旧偏好。
- 本机持久化与远程使用授权是两件事。默认不允许远程处理，许可必须同时约束模型、工具和 embedding；删除/停用后不能由旧索引或重开重新复活。

## 退出、备份与恢复

- 普通关闭先核对未保存草稿和活动请求，再由唯一 owner 管理器 drain/停止本 App 的进程；状态未知不能假称已保存或已取消。
- 从设置选择备份目标，等待 COMPLETE 与真实文件清单/checksum。失败时不要手动篡改清单、删除校验或伪造缺失 owner。
- 恢复先在隔离目标验证，不直接覆盖原资料。只有明确确认后才激活；旧会话失效、个人记忆确认与请求代际规则必须保留。隔离验证通过并不等于已激活。
- 修复后的 SQLite 备份在受保护副本上处理 main/WAL/SHM，避免备份读取触发原库 checkpoint；真实备份恢复证明及失败历史以验收记录为准。
- App 重开与电脑重启是两种证据。不要为了验证而擅自重启用户电脑或终止不明进程。

## 独立清除入口（正在完成宿主/最终执行者验收）

该操作独立于卸载、升级、备份和恢复。设置以及本机登录前的“管理/清除本机数据”入口调用同一个受控协议，不读取未登录账号的论文或记忆内容。

1. 先保存或明确放弃其他草稿、等待任务安全结束。预览会先安全停止本 App 服务，但不删除业务资料。 普通预览后的取消不会自动恢复服务；可正常关闭再重开 App 恢复，且不会重放业务请求。若已进入 PREPARED、部分失败或未知维护状态，不能把普通重开当成恢复成功。
2. 阅读实际范围、保留项、凭据策略与到期时间。未知归属、路径置换、过期/已消费的预览必须重新核对，不可手动绕过。
3. 逐字输入 **删除本机全部 Agora 数据**，再通过默认“取消”的独立原生确认。
4. PREPARED 仅表示已准备；它不是“数据已删除”。固定执行者必须在精确宿主进程退出后，清理实际整个 profile 和专属凭据并核验完成，才能记录 CLEARED。
5. 维护回执位于目标之外，只保留最小操作身份摘要/状态，不保留账号内容、密钥或论文内容。部分失败或结果未知时，不自动继续删除、不启动新的业务 writer，也不创建空资料冒充成功；通过明确维护入口重新确认或按诊断处理。
6. 只有确认 CLEARED 后正常重开并重新出现首次账号创建，才构成完整清除闭环。未登记归属的旧目录不应由预览静默接管。

不要用 shell 手工删除资料根来替代该产品流程。测试只能清除本轮明确新建、已登记并经过相同确认的测试 profile；不得清理已有用户资料。

## 开发与候选构建

在仓库根使用项目 Python，Electron 工具使用本仓库固定的依赖。当前已实测的 managed Node 为 24.21.0；依赖版本以锁文件和最终资源清单为准，不随意更新。

```powershell
npm --prefix frontend test -- --run
npm --prefix frontend run lint
npm --prefix desktop run typecheck
npm --prefix desktop test
npm --prefix desktop run build
openspec validate paper-agent-desktop-app --strict
python -m scripts.dev smoke
```

前端默认 TypeScript 配置可能不包含全部 feature 文件；验收还须显式覆盖正式 feature 与其测试，不能只运行默认 typecheck 就声称全部通过。

候选打包须先由管理命令产生完整 portable runtime stage，再设置 `AGORA_DESKTOP_RUNTIME_STAGE` 并运行 `npm --prefix desktop run dist`。该命令使用已冻结资源清单进行独立、排他复制和多阶段完整校验，不接受缺文件/改 hash/硬链接/未列出资源或预打包绕过。代码签名如需改变 runtime 字节，必须在源资源清单形成前完成，不能在打包后重写清单。

Windows NSIS 候选仍使用 electron-builder 的标准 installer、升级和 uninstaller 流程，但解压阶段通过官方 `nsis.include/customHeader` 与单文件 `installer.nsh` shim 接入。构建会锁定 app-builder-lib 26.15.3 的四个上游模板、现代 7za 24.09（SHA-256 `223b873c50380fe9a39f1a22b6abf8d46db506e1c08d08312902f6f3cd1f7ac3`）、stage Node、bootstrap、archive 和完整 payload manifest；摘要或 lock 漂移直接阻断。现代 decoder 直接写最终安装树，不再经过旧 Nsis7z/CopyFiles；完整 Electron payload 与 `resources/runtime` 的目录、文件集合、size、SHA、hardlink/reparse 和 extra/stale gate 全部通过并封存安装归属后，才进入标准注册表、快捷方式和启动步骤。7-Zip 的 `COPYING` 与 `LICENSE.txt` 完整随应用资源交付。

安装目标在 stock 旧版本卸载前和实际解压入口各校验一次：只接受缺失/完全空目录、当前安装器已知且摘要匹配的浅层 pre-file，或完整 manifest、owner、uninstaller 摘要均可复核的同产品安装；未知文件、别名/链接、hardlink、身份变化或部分树一律失败，不自动接管。卸载使用官方窄 `customRemoveFiles`，仅删除已完整复核的安装树；独立 Electron `userData`、导入原件、导出和备份不在该删除范围。升级会先完整预验新 payload 与旧安装归属，再调用 stock 旧 uninstaller；旧卸载或新安装失败会如实失败，不宣称原子 rollback。Windows 上 Node 能识别的 symbolic link/junction 会被拒绝；其他无法由 Node `lstat` 分类的特殊 reparse tag 不被声称已覆盖，发布验收仍须保留真实原生长路径升级/卸载检查。

每次 `preflight`、旧安装归属检查、`install`、`seal` 或 `uninstall` 都启动一个独立 bootstrap invocation；每次 invocation 有 **15 分钟真实 wall-clock deadline**，其 7za 子进程另有最多 5 分钟且受该总 deadline 截断的上限，超时会终止活动子进程并以非零退出。NSIS 的 `nsExec /TIMEOUT=960000` 只是 16 分钟的插件无输出 watchdog，不是 wall deadline；bootstrap 捕获且不向 NSIS 转发 7za stdout，因此不能靠持续输出延长 Node 的 wall budget。整个安装/升级当前没有跨 invocation 共享的一只总计时器：相关阶段和 stock 旧卸载的有界重试会顺序累加，但每次均有固定上限。任一阶段失败都保持非成功/可能部分完成的真实状态，不继续注册或启动，不宣称回滚。卸载删除计划形成后，每个文件、目录及其祖先在实际删除前还会再次核对 plain/type/dev/ino/size/link identity；发生树替换会中止并保留可诊断的 partial 结果。

最终交付必须包含真实安装包、checksum、source/release/config/dependency 身份、实际签名状态和中文限制说明。当前不承诺已安装、已签名、免安全提示或已完成非开发环境验收。
