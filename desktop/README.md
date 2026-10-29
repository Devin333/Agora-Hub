# Agora Paper Agent 桌面客户端

桌面端只是工作台的窗口：论文库、账号、模型调用和所有资料都在它连接的服务上，客户端本身不运行 Python、数据库或 Next 服务，也不保存论文、笔记、记忆或模型密钥。窗口直接打开服务上的 `/paper-agent` 工作台，登录由服务的账号体系负责（管理员创建账号，每个用户在账号设置里填写自己的模型 key）。

## 三种连接方式

首次启动时在连接页选择，之后可以从托盘“切换连接方式…”更改：

- **官方服务**：构建时用 `AGORA_DESKTOP_OFFICIAL_URL` 写入官方地址才显示，并作为默认选项；没有写入时隐藏。
- **连接已有服务器**：填写团队自己部署的服务器地址，部署见 `docs/hosted-deployment.md`。
- **在这台电脑上运行**：用本机的 Docker 运行和服务器相同的 api、web 镜像，数据只保存在这台电脑上。要求、数据位置、备份和卸载见 `docs/local-mode.md`。

选择保存在 `userData/host-preferences/connection.json`（`agora.desktop-connection/v2`，记录方式和最近一次的服务器地址；旧版保存的地址会自动迁移）。

## 连接服务器

- 打开服务器或官方服务前先向 `/api/health` 发一次探测，确认对方是就绪的托管版 Agora，再在同一窗口打开工作台；探测不跟随重定向、不使用缓存。
- 地址必须是 HTTPS 的纯 origin（不含路径、账号密码）。只有未打包的开发运行允许 `http://localhost` / `127.0.0.1` / `[::1]`。
- 保存新选择前会先验证；地址错误、不是 Agora 或证书无效时直接在表单里说明原因，不会保存。
- 连接失败时显示连接页，说明原因：网络断开、DNS、拒绝连接、超时、连接被重置、代理和服务器 5xx 会按 2、5、10、30、60 秒自动重试，网络恢复时立即重试；证书/TLS 错误、地址不是 Agora、服务器把工作台重定向到其他站点时等待用户处理。已打开的工作台如果之后返回 5xx 或页面崩溃，也会回到连接页并自动重连。
- 切换连接方式会先核对未保存草稿和进行中的请求，确认后才离开工作台。

## 本机服务

- 由 `src/local-stack.ts` 管理，compose 项目名 `agora-desktop`，文件在 `userData/stack`：打包进来的 `compose.images.yml`、由 `env.template` 生成的 `.env`（权限 0600，密钥只生成一次）和 `state.json`（镜像下载源、管理员是否已创建）。
- 启动顺序：检查 Docker 和 Compose → 写配置、选端口（默认 45180，被占用时换空闲端口）→ 下载镜像（固定版本已存在时跳过，`edge` 每次重新拉取）→ `up --wait` → 用身份接口确认服务 → 没有账号时在连接页创建管理员（`news accounts create-admin --password-stdin`）。
- 已在运行且版本一致时走快速路径：2 秒内确认身份后直接打开工作台。身份接口是 web 镜像里的 `/.well-known/agora-desktop-stack`，需要 `.env` 里的令牌，并返回镜像构建时写入的版本号，防止端口上是其他程序或旧版本的服务。
- 调用 Docker 时只传递白名单内的环境变量，模型 API key 等不会进入 Docker 命令；超时或取消时结束整个进程树。容器在客户端退出后继续运行，托盘可以停止服务或查看日志（已去掉密钥）。

## 安全边界

- 窗口只显示打包的连接页和当前服务器的 `/login`、`/paper-agent…` 页面。其他 http(s) 链接交给系统浏览器，同一服务器的其他页面和非 http(s) 协议一律拒绝；新窗口、下载、webview 和权限请求全部拒绝。
- 渲染进程没有 Node。工作台页面只能拿到 `window.agoraDesktop`（退出确认、导出保存、外观），连接页只能拿到 `window.agoraConnection`；主进程按发送方的主框架 URL 再次核对，子框架调用一律拒绝。

## 关闭与退出

- 关闭窗口只隐藏到通知区域托盘，工作台保持加载，再次打开 App 或单击托盘图标立即回到同一窗口。托盘可勾选“登录 Windows / Mac 时在后台启动”（仅安装版）。通知区域图标创建失败时，关闭窗口走完整退出流程。
- 托盘“退出”或系统注销/关机才真正退出。退出前核对未保存草稿和进行中的请求；离开后草稿丢失、请求中断且不会自动重放，因此有这些内容时会要求确认。

## 开发与构建

```powershell
npm --prefix desktop run typecheck
npm --prefix desktop test
npm --prefix desktop run test:e2e
```

e2e 在本机回环地址启动一个模拟 `/api/health` 和 `/paper-agent` 的服务，覆盖连接页、桥接隔离、导航限制、单实例和托盘隐藏/恢复。测试配置（`--agora-test-user-data-dir`）的本机服务使用独立的 compose 项目名 `agora-desktop-test-<哈希>`，不会碰到已安装客户端的本机服务。

`e2e/local-stack.spec.ts` 用真实 Docker 跑完整的本机模式：启动、创建管理员、登录、重新打开。它需要本机已有的镜像，默认跳过。在仓库根目录先构建镜像（web 镜像报告的版本号必须和标签一致）：

```bash
docker build -f deploy/hosted/api.Dockerfile -t local/agora-api:v0.0.0-check .
docker build -f deploy/hosted/web.Dockerfile --build-arg AGORA_IMAGE_TAG=v0.0.0-check -t local/agora-web:v0.0.0-check .
```

然后运行：

```powershell
$env:AGORA_DESKTOP_E2E_IMAGE_REGISTRY = 'local'
$env:AGORA_DESKTOP_E2E_IMAGE_TAG = 'v0.0.0-check'
npm --prefix desktop run test:e2e
```

测试结束后会删除它创建的容器、网络和数据卷。

开发时连接本机前端：先在 `frontend` 以托管 profile（`NEWSROOM_DEPLOYMENT_PROFILE=hosted`，并配置 API 地址与公开 origin）运行 Next，再运行 `npm --prefix desktop run build` 后用 `npx electron desktop` 启动，选择“连接已有服务器”并填 `http://localhost:3000`。开发运行的本机模式默认使用 `edge` 镜像，可以用 `AGORA_DESKTOP_IMAGE_TAG`、`AGORA_DESKTOP_IMAGE_REGISTRY` 改成其他标签或仓库（只对未打包的运行生效）。

## 打包

构建时读取的变量，写入 `out/build.json`：

| 变量 | 作用 |
| --- | --- |
| `AGORA_DESKTOP_OFFICIAL_URL` | 官方服务地址（HTTPS origin）。不设置时隐藏“官方服务”选项。旧变量 `AGORA_DESKTOP_SERVER_URL` 仍然有效 |
| `AGORA_DESKTOP_IMAGE_TAG` | 本机模式运行的镜像标签。不设置时安装包使用 `v<客户端版本号>`，与同版本发布的镜像对应 |

```powershell
npm --prefix desktop run dist       # Windows NSIS 安装包
npm --prefix desktop run dist:mac   # macOS arm64 dmg，需在 Mac 上执行
```

发布客户端前先确认同版本号的镜像（`v<版本号>`）已经推送到两个镜像仓库，否则用户选择本机模式时会提示镜像不存在。安装包使用 electron-builder 的标准安装、升级和卸载流程；卸载只删除安装目录，`userData` 中的连接方式、外观偏好和本机服务配置保留，本机服务的容器和数据卷也保留（彻底删除见 `docs/local-mode.md`）。
