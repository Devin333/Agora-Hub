# Research 本机运行工具

> 当前交付状态：第 12 章实际生产版本为 `20261013T142952Z-1006169e2757`，可从既有桌面入口或 `http://127.0.0.1:3000` 访问。启动、重复启动、停止、端口冲突、异常退出/清理、restart、日志轮换和两轮全部 42,383 个资源严格校验通过。共享工作区最终测试期间发生并行源码/测试变动；共享 smoke（3 failed、4,189 passed）及共享 full（1 failed、9,245 passed）已自然退出，结果保留为历史，full 的超时测试根因已确认，test-only 补丁已私有验证并集成到最终隔离 worktree `.tmp/dvf`；原隔离 smoke 自然通过（4,192 passed），full 自然失败（1 failed、9,245 passed、117 skipped），唯一失败为未跟踪的原始 compatibility policy 未复制到 worktree。最终 `.tmp/dvf` 已补齐原字节 pending policy、绑定非 Python 输入并集成测试修复，完整 full/smoke 已启动，等待自然结果。真实账号尚未初始化，配置模型不具备 Research schema 资格；标准备份因必需资料 owner 未初始化被拒绝且无归档。认证 PDF/分析/问答/报告恢复、实际 OS 缩放及电脑重启未验收，实例状态 DEGRADED。

在 PowerShell 中从仓库根执行：

```powershell
.\scripts\local-desktop.ps1 doctor
.\scripts\local-desktop.ps1 --json doctor
```

入口始终使用 `.venv\Scripts\python.exe`，不会回退到系统 Python，不会自动安装依赖、下载模型、修改真实 `.env`、重启电脑或注册系统服务。`local-desktop` profile 只接受 IPv4 loopback bind；默认浏览器入口为 `http://127.0.0.1:3000`，API 为 `http://127.0.0.1:8000`。

常用命令：

```powershell
.\scripts\local-desktop.ps1 doctor
.\scripts\local-desktop.ps1 doctor --probe-network --timeout 10 --max-requests 2 --max-input-tokens 256 --max-output-tokens 64 --max-cost-usd 0.02
.\scripts\local-desktop.ps1 build
.\scripts\local-desktop.ps1 start --open
.\scripts\local-desktop.ps1 status
.\scripts\local-desktop.ps1 logs --role api --stream stderr --lines 200
.\scripts\local-desktop.ps1 stop
.\scripts\local-desktop.ps1 backup
.\scripts\local-desktop.ps1 restore <backup.zip> --target-root <isolated-directory>
.\scripts\local-desktop.ps1 restore <backup.zip> --target-root <isolated-directory> --activate
.\scripts\local-desktop.ps1 rollback <release-id>
.\scripts\local-desktop.ps1 desktop-entry status
.\scripts\local-desktop.ps1 desktop-entry install
.\scripts\local-desktop.ps1 desktop-entry remove
.\scripts\local-desktop.ps1 autostart status
.\scripts\local-desktop.ps1 autostart enable
.\scripts\local-desktop.ps1 autostart disable
```

`desktop-entry` 与 `autostart` 默认都不启用。桌面入口只写当前用户 Desktop 下由 instance identity 派生的固定名称 `.lnk`，target 固定为本仓库 `scripts/local-desktop.ps1`，参数固定为 `start --open`，以隐藏 PowerShell 窗口启动；安装、移除或覆盖前都校验 target、arguments、working directory 与 instance ownership。`autostart enable` 同样拒绝覆盖同名但值不属于当前 instance 的注册表项，不创建系统服务。

配置优先级为：命令行显式参数 > `.newsroom/desktop/config.json` > 仓库根 `.env` > 内建默认值。 CLI 支持 `--profile`、frontend/API host/port、`--canonical-origin`、`--api-base-url`、runtime/release/backup/control 路径、`--parser-mode`、`--data-root` 和 `--artifact-root`，可以放在命令前后；CLI 参数不会写入配置文件。使用临时覆盖构建、启动、停止或诊断同一实例时，应传递同一组端口、origin 与存储参数。readiness/drain/process timeout 必须为有限正数，最大 300 秒。profile 只允许精确值 `local-desktop`；`NEWSROOM_CANONICAL_ORIGIN` 与兼容别名冲突时拒绝启动。所有存储路径在原仓库根解析并绝对化，release 运行时继续指向真实 owner root；原始 `.env`、`.env.*` 与 secret 不会复制进 release。指向项目 `configs/...` 的 `NEWS_RESEARCH_MODELS_CONFIG`/`NEWS_MODELS_CONFIG`/`NEWS_SOURCES_CONFIG` 会随源码打包到 release，并在启动时改指向 release 内的固定副本；项目外显式配置属于外置非秘密依赖，其绝对路径、大小和 checksum 会绑定到 release，文件漂移后 active release 校验失败。Research 的 `NEWS_RESEARCH_MODELS_CONFIG` 优先于通用 `NEWS_MODELS_CONFIG`；两者都受同一 release 绑定与漂移校验。未显式指定时使用 release 内打包的 `app/configs`。

`doctor` 默认离线，只读取解释器、Node/npm、lockfile、ResearchRuntimeSettings 解析出的 source/model/storage、磁盘、端口和选定 parser 条件。`pymupdf` 直接使用已有 Python runtime，不要求 Docker；`marker` 检查既有 `ResearchParserExecutionAdapter` 使用的 execution profile、Docker provider、image、cache 与 `network=deny` 条件，不下载 image/model，也不另起 Docker manager。当前桌面支持的 parser mode 为 `pymupdf` 与 `marker`。缺少 Docker 只影响所选容器 parser。显式网络探测有超时、请求数、token 和费用上限；它只能证明 source/provider 的可达性。真实 PDF、单论文分析和问答只有经过对应 application 流程验收后才能标记为 `verified`。

`build` 是唯一允许准备依赖和生成 Next.js production build 的命令。它生成 release manifest，记录源码摘要、Git revision、Python resolved distributions、Python/Node/npm 二进制与版本、frontend lockfile、配置身份及每个 release 文件的 checksum。`start` 只运行已原子激活且重新校验完整的 release。生产 profile 拒绝启用 `NEXT_PUBLIC_NEWSROOM_USE_MOCKS`，release 的配置身份必须声明 `data_mode=real`；实际 Node 依赖树如有 missing、invalid 或 extraneous package，会拒绝构建或启动。

`stop` 先通过 application control port 停止新 admission，并等待当前有界业务操作收敛。若 control port 不可用或仍有活跃操作，命令返回 busy，且不会把业务 run 标成取消/完成，也不会按端口或进程名杀死程序。只有 PID、创建时间和 executable 均匹配的 owned process 才会被停止。

`backup` 不信任历史 `instance.json` 的停止状态；在读取任何 owner 前，它还会通过同一个 application control authority 执行 `begin_drain`，确认 active writer 为零，并持续关闭 admission 直到所有 owner snapshot 与 manifest 写完，之后按原状态恢复 admission。它从实际环境解析 auth、session、workspace/history、imports、Research/Harness artifacts/events/index 及 desktop admission control metadata。JSON 使用 owner lock，SQLite 使用原生 backup 并覆盖 WAL 状态；未知 writer、锁冲突、链接/junction、秘密文件或资源上限失败时不会生成有效备份。provider credentials、原始 `.env`、缓存、模型、依赖、release 和内容日志默认排除。

`restore` 默认只解包到隔离 root，并用只绑定 staging root 的原 application adapters 读取 auth user/session、workspace/history、imports、Research run/latest/diagnostic、Harness artifacts/terminal manifests、catalog paper/document/evidence、local chunks、RAG transcript/event journal，以及 graph result、side-effect、node-output SQLite。它同时校验跨 owner 的 session/user、run/artifact、chunk/paper/document 引用，并拒绝 dangling index、运行路径回跳原 root、SQLite operational path 与不可安全重定位的实际 asset path。`source_ref` 一类不可变 evidence anchor 保留原 identity，但不会作为恢复后的文件 I/O 路径；可读私有 PDF 只通过已重定位的 import owner path 解析。manifest 中的原数据路径仅作证据，不作为恢复目标；目标路径由当前受信 inventory 重新解析。成功激活会清空恢复会话、通过既有 Research desktop control store 旋转 fresh generation，并把旧非终态请求 fenced 为 `needs_review`，不会自动重放或把 not-found 当作重试许可。

`rollback` 只接受资源与依赖完整、且声明可读取当前 data schema 的 release。不兼容时拒绝原地降级；只有显式给出兼容备份时才按完整 restore 合同切换数据，不执行 Git 工作树回滚、逆迁移或删除用户资料。


当前验证范围：Windows ACL 已在隔离临时目录通过自动合同测试，包含 Everyone 权限移除、备份输出父目录与相邻文件权限不变，以及敏感资料写入前的权限保护。这不替代真实资料和完整部署验收。快捷方式 COM、自有进程、loopback 端口和日志轮换已有第 12.2/12.8 的实机记录；模型/provider、真实账号与资料的完整隔离恢复仍需在 OpenSpec 第 12 章按真实配置执行；本机重启须由用户实际完成。本文描述实现与自动检查范围，不表示第 12 章验收已经通过。


根 `.env` 含其他模块使用的 PostgreSQL DSN，而本机 Research 使用 owned JSON/SQLite 时，可在 `.newsroom/desktop/config.json` 明确设置：

```json
{
  "deployment_profile": "local-desktop",
  "storage_mode": "owned-local",
  "parser_mode": "pymupdf"
}
```

`storage_mode` 只接受 `root-environment`（默认）或 `owned-local`，没有根环境变量别名。`owned-local` 只为桌面子进程隔离 DSN，根 `.env` 保持原样；显式启用 PostgreSQL-only memory 会报冲突。改变该模式后须重新 build，旧 release 不能沿用。首次缺少 required owner 会得到未初始化提示，正式 backup 仍要求真实产品流程建立对应存储。

本次 catalog 修复使新 chunk manifest artifact 持久化 owner-relative 路径。旧 artifact 仅允许通过受信原 owner root 映射到同 scope/paper 的 staged target，保持原 digest 和 bytes；无法建立该映射的绝对路径仍拒绝。物理保护快照不等于正式 backup/restore 验收。


2026-10-13 的真实配置核对确认：模型路由有 API Key，但当前部署没有 approved、enabled 且匹配 `research.candidate` 的 native structured-output release，且没有部署定价。单论文分析与问答尚未取得业务验证，`available=false`；未发起付费模型调用。供应商认证/模型清单 GET 可达不能替代真实业务验收。完成模型身份、资格与有界费用配置后，仍须重跑第 12.6。来源、PDF、笔记和分组只按各自真实验收结果声明。降级启动不能关闭第 12.13 的“最终已验收构建”或全部 AC。

真实账号必须由用户在生产登录页面输入账号和口令，验收程序不生成固定账号、不读取口令、不把 session/cookies 写入提交证据。电脑重启也须由用户实际进行；服务 restart 不等于 AC-15 通过。

production release 的完整性检查也覆盖前端预渲染资源。本机 profile 的运行时页面缓存不能写入发布目录；若诊断报告 `desktop_release_resource_inventory_mismatch`，应保留该失败版本和日志，修复根因并重新 build，不手动删缓存或修改 manifest 来恢复校验。

审计、通用 artifact 与 parser 临时目录也属于本机运行时写入。默认 audit 位于 `.newsroom/desktop/audit/audit.jsonl`，parser run 位于 `.newsroom/desktop/parser-runs`；通用 artifact 默认使用同一受信 Research artifact root。显式路径配置继续优先，相对值以项目根解析。日志/资料不应写入不可变 release app，诊断不得通过忽略额外文件来宣布发布完整。
