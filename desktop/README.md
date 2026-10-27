# Agora Paper Agent 桌面客户端

桌面端是托管服务的客户端：论文库、账号、模型调用和所有资料都在服务器上，本机不运行 Python、数据库或 Next 服务，也不保存论文、笔记、记忆或模型密钥。窗口直接打开服务器上的 `/paper-agent` 工作台，登录由服务器的账号体系负责（管理员创建账号，每个用户在账号设置里填写自己的模型 key）。服务器部署见 `docs/hosted-deployment.md`。

## 连接服务器

- 启动后先向服务器的 `/api/health` 发一次探测，确认对方是就绪的托管版 Agora，再在同一窗口打开工作台；探测不跟随重定向、不使用缓存。
- 服务器地址按以下顺序取得：用户在本机保存的地址（`userData/host-preferences/connection.json`）、构建时写入的默认地址（`out/server.json`）、都没有时显示地址表单。
- 地址必须是 HTTPS 的纯 origin（不含路径、账号密码）。只有未打包的开发运行允许 `http://localhost` / `127.0.0.1` / `[::1]`。
- 保存新地址前会先探测；地址错误、不是 Agora 或证书无效时直接在表单里说明原因，不会保存。与构建默认地址相同的地址不另存，可用“恢复默认地址”清除本机设置。
- 连接失败时显示连接页，说明原因：网络断开、DNS、拒绝连接、超时、连接被重置、代理和服务器 5xx 会按 2、5、10、30、60 秒自动重试，网络恢复时立即重试；证书/TLS 错误、地址不是 Agora、服务器把工作台重定向到其他站点时等待用户处理。已打开的工作台如果之后返回 5xx 或页面崩溃，也会回到连接页并自动重连。
- 托盘菜单“更换服务器地址…”会先核对未保存草稿和进行中的请求，确认后才离开工作台。

## 安全边界

- 窗口只显示打包的连接页和当前服务器的 `/login`、`/paper-agent…` 页面。其他 http(s) 链接交给系统浏览器，同一服务器的其他页面和非 http(s) 协议一律拒绝；新窗口、下载、webview 和权限请求全部拒绝。
- 渲染进程没有 Node。工作台页面只能拿到 `window.agoraDesktop`（退出确认、导出保存、外观），连接页只能拿到 `window.agoraConnection`；主进程按发送方的主框架 URL 再次核对，子框架调用一律拒绝。

## 关闭与退出

- 关闭窗口只隐藏到通知区域托盘，工作台保持加载，再次打开 App 或单击托盘图标立即回到同一窗口。托盘可勾选“登录 Windows 时在后台启动”（仅安装版）。通知区域图标创建失败时，关闭窗口走完整退出流程。
- 托盘“退出”或系统注销/关机才真正退出。退出前核对未保存草稿和进行中的请求；离开后草稿丢失、请求中断且不会自动重放，因此有这些内容时会要求确认。

## 开发与构建

```powershell
npm --prefix desktop run typecheck
npm --prefix desktop test
npm --prefix desktop run test:e2e
```

e2e 在本机回环地址启动一个模拟 `/api/health` 和 `/paper-agent` 的服务，覆盖地址表单、连接页、桥接隔离、导航限制、单实例和托盘隐藏/恢复。

开发时连接本机前端：先在 `frontend` 以托管 profile（`NEWSROOM_DEPLOYMENT_PROFILE=hosted`，并配置 API 地址与公开 origin）运行 Next，再运行 `npm --prefix desktop run build` 后用 `npx electron desktop` 启动，在地址表单填 `http://localhost:3000`。

为用户构建安装包时写入默认服务器地址：

```powershell
$env:AGORA_DESKTOP_SERVER_URL = 'https://agora.example.com'
npm --prefix desktop run dist
```

不设置该变量时安装包首次启动会询问地址。安装包使用 electron-builder 的标准 NSIS 安装、升级和卸载流程；卸载只删除安装目录，`userData` 中的连接地址和外观偏好保留。
