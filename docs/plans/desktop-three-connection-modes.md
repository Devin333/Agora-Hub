# 桌面端三种连接模式改造方案

状态：待确认（2026-10-29）。确认前不改代码。

## 1. 背景

桌面端目前只能连一个 HTTPS 服务器。参考 Rakazo 的做法，改成三种连接模式：

| 模式 | 界面名称 | 用户做什么 | 服务跑在哪里 |
|---|---|---|---|
| 官方服务 | 官方服务 | 直接登录 | 我们部署的服务器 |
| 自建服务器 | 连接已有服务器 | 填管理员给的 HTTPS 地址 | 公司或个人自己部署的服务器 |
| 本机运行 | 在这台电脑上运行 | 装好 Docker，点“开始” | 用户本机 Docker，监听 `127.0.0.1` |

三种模式用**同一套服务端镜像**（`agora-api`、`agora-web`），只是编排方式不同。桌面端始终是瘦客户端，只负责打开网页；本机模式额外负责管理本机的容器。

## 2. 现状（以 `Devin/cool-euclid-e089ff` 分支为准）

- **桌面端瘦客户端已完成**（提交 `618337ad`）。地址校验在 `desktop/src/server-origin.ts`，健康检查在 `server-probe.ts`，重连状态机在 `workspace-connection.ts`，导航白名单在 `navigation-policy.ts`。
- **限制 1**：打包后的版本只接受 HTTPS，连 `127.0.0.1` 也不行（`desktop/src/main.ts:256`，`allowLoopbackHttp: !app.isPackaged`）。
- **限制 2**：连接页只有“填地址”这一个选项，没有官方地址常量，只能在构建时通过 `AGORA_DESKTOP_SERVER_URL` 写入 `out/server.json`。
- **服务端部署已完成**（提交 `0b39780a`）。`deploy/hosted/compose.yaml` 包含 caddy、web、api 三个服务，数据都是 `newsroom-data` 卷里的文件和 SQLite，**没有** Postgres、Qdrant、Redis，也不用显卡。PDF 解析强制用 PyMuPDF。
- **限制 3**：镜像在服务器上从源码构建（`docker compose up --build`），仓库没有发布镜像的 CI；`api-constraints.txt` 只锁了 x86_64 的依赖。
- **限制 4**：compose 写死了子网 `172.30.10.0/24` 和可信代理 `172.30.10.20/32`。
- **账号**：只能用命令行 `news accounts create-admin` 创建，hosted 模式没有网页初始化入口。
- **本机运行的现成支持**：hosted 配置已支持 `NEWSROOM_PUBLIC_ORIGIN=http://127.0.0.1:<端口>` 加 `NEWSROOM_ALLOW_INSECURE_ORIGIN=1`，此时 Cookie 不带 `Secure`，可以在本机用 HTTP 登录。但 BFF 要求请求的 `Host` 与这个地址**完全一致**（`frontend/src/lib/auth/portal-server.ts:36-43`），`localhost` 和 `127.0.0.1` 不能混用。
- **模型 key**：每个账号一个，AES-256-GCM 加密存储，所有大模型调用都用当前账号的 key（`NEWSROOM_LLM_CREDENTIAL_MODE=invocation`）。可选的服务商来自 `configs/hosted-llm-providers.yaml`，用户不能自定义地址。
- **分支**：这些提交都还没进 main，`Devin/cool-euclid-e089ff` 比 main 领先 43 个提交，合进 main 没有冲突。`codex/e2e-answer-evaluation` 上另有一套“模型连接”领域模型，能按用途路由、带防 SSRF 校验，但没接到任何调用点；它和 cool-euclid 互相合并会在 12 个文件上冲突。

## 3. 总体设计

```
                 同一套镜像：agora-api / agora-web（发布到镜像仓库）
        ┌──────────────────────────┴──────────────────────────┐
deploy/hosted/compose.yaml                        deploy/desktop/compose.images.yml
caddy(HTTPS) → web → api                          web(127.0.0.1:端口) → api，无 caddy
模式 1 官方服务、模式 2 自建服务器                 模式 3 本机运行（桌面端管理）
```

- 模式 1 和模式 2 在桌面端都是“连一个 HTTPS 地址”，区别只是地址从哪来：官方地址在构建时写入，自建地址由用户填写。
- 模式 3 仍用 hosted 配置，只是打开 `NEWSROOM_ALLOW_INSECURE_ORIGIN=1`，公开地址设为 `http://127.0.0.1:<端口>`。服务端不新增运行模式，三种模式走同一套账号、权限和 key 逻辑，避免分叉。
- 本机模式的地址只认桌面端自己拉起的那一套服务：精确匹配地址，并用 token 验证身份。不放开“任意本机 HTTP 地址”。

## 4. 实施步骤

按依赖顺序排列。第 0 步完成后，第 1、2、3 步可以并行。

### 第 0 步：合并基线

- 把 `Devin/cool-euclid-e089ff` 合进 main（已验证无冲突）。该分支所在的工作树里有未提交的改动（`configs/models.yaml`，以及 `.tmp-e2e/`、`.tmp-edit/` 两个目录），不动它们，合并前先问你怎么处理。
- `codex/e2e-answer-evaluation` 的模型连接整合是另一项独立任务，不是三种模式的前提。原因见第 6 节第 4 条。

### 第 1 步：镜像发布与自建服务器安装

1. **发布镜像的 CI**：新增 `.github/workflows/images.yml`，构建并推送 `agora-api` 和 `agora-web`。
   - 标签：`v<版本号>`（发布版本）、`edge`（main 最新）、`sha-<短哈希>`。
   - 仓库：见第 6 节第 1 条。
2. **多架构**：把 `deploy/hosted/lock_api_constraints.py` 扩展为同时锁 linux/amd64 和 linux/arm64，CI 用 buildx 构建两种架构。Apple 芯片的 Mac 用户需要 arm64 镜像，否则只能靠模拟运行，速度很慢。
3. **compose 改为拉取镜像**：`deploy/hosted/compose.yaml` 改用 `image: ${AGORA_IMAGE_REGISTRY}/agora-api:${AGORA_IMAGE_TAG}`；从源码构建的配置拆到 `deploy/hosted/compose.build.yaml`，作为覆盖文件按需叠加。
4. **子网可配置**：子网和 web 的固定 IP 改由环境变量 `AGORA_APP_SUBNET`、`AGORA_WEB_IP` 设置，`NEWSROOM_TRUSTED_PROXY_CIDRS` 由它们推导，默认值保持现在的不变。
5. **安装脚本**：新增 `deploy/hosted/install.sh`。它下载 compose、Caddyfile 和 `.env` 模板，生成两把密钥（各自用正确的编码，见第 5 节），询问域名，然后拉取镜像、执行 `up -d --wait`，最后提示创建管理员。重复执行时保留已有的 `.env`。
6. **给镜像瘦身（先核实）**：服务端的代码路径构建阅读问答服务时传的是 `with_reranker=False`（`interfaces/composition/research.py:1744-1757`），重排模型可能从来没被加载过。如果核实属实，就从镜像里去掉 `rerank` 依赖组和 torch，镜像能小好几 GB，本机模式的首次下载量也跟着降下来。核实方法：查看运行中容器的日志和 `hf-cache` 卷里有没有下载过模型。

### 第 2 步：服务端支持本机模式

1. **服务身份接口**：前端新增 `GET /.well-known/agora-desktop-stack`。只有请求头 `x-agora-desktop-stack-token` 与环境变量 `AGORA_DESKTOP_STACK_TOKEN` 一致时（用恒定时间比较），才返回 `{ok: true, imageTag}`，否则返回 404。没设置这个环境变量时（官方和自建服务器）一律 404。
2. **本机编排文件**：新增 `deploy/desktop/compose.images.yml`。
   - 只有 api 和 web 两个服务，不要 caddy。
   - web 映射到 `127.0.0.1:${AGORA_WEB_PORT}`；api 不映射端口。
   - 不设固定子网，`NEWSROOM_TRUSTED_PROXY_CIDRS` 留空：本机只有一个用户，按客户端地址限流在这里没有意义。需要先核实 `load_hosted_settings` 接受空值。
   - 加 `restart: unless-stopped`，数据卷名带上 `agora-desktop` 前缀。
   - 加固：`no-new-privileges`，`cap_drop: ALL`，并设置内存上限。
3. **环境变量模板**：新增 `deploy/desktop/env.template`，其中密钥项留空，由桌面端生成后填入；`NEWSROOM_ALLOW_INSECURE_ORIGIN=1`，`NEWSROOM_PUBLIC_ORIGIN` 由桌面端按实际端口填写。
4. **创建管理员**：沿用现有命令 `news accounts create-admin <用户名> --password-stdin`。桌面端执行 `docker compose exec -T api ...`，把密码通过标准输入传进去，不出现在命令参数里。不新增网页初始化入口，hosted 模式下没有初始化入口的规则保持不变。

### 第 3 步：桌面端三种模式

**连接配置**（`desktop/src/server-connection.ts`）
- 配置格式升级到 `agora.desktop-connection/v2`，内容为 `{mode: "official" | "server" | "local", origin}`。
- 从 v1 自动迁移：用户填过的地址迁成 `server`，构建时写入的地址迁成 `official`。
- 官方地址由构建变量 `AGORA_DESKTOP_OFFICIAL_URL` 写入，旧变量 `AGORA_DESKTOP_SERVER_URL` 继续兼容。

**地址信任**（`server-origin.ts`、`navigation-policy.ts`、`ipc.ts`、`server-probe.ts`）
- `official` 和 `server` 两种模式仍然只接受 HTTPS。
- `local` 模式只接受本机服务控制器当前管理的那个地址，必须精确匹配 `http://127.0.0.1:<端口>`，并且身份接口验证通过。
- 把“受管本机地址”作为参数传给地址校验、导航白名单和 IPC 来源检查，不放开“任意本机 HTTP 地址”。
- 每个地址用独立的会话分区（`persist:agora-<地址哈希>`），切换模式时不同服务器的登录状态互不干扰。

**新增模块**（参考 Rakazo 的 `docker-cli.ts` 和 `local-stack.ts`）
- `desktop/src/docker-cli.ts`：
  - 查找 docker 可执行文件。Windows 先查 `%ProgramFiles%\Docker\Docker\resources\bin`，再查 PATH；macOS 和 Linux 查常见安装路径。
  - 启动子进程时只传白名单里的环境变量，避免把用户 shell 里的 API key 带进容器。
  - 执行命令带超时，超时后结束整个进程树（Windows 用 `taskkill /t /f`）。
  - 把失败原因归类：Docker 没装、没启动、没权限，端口被占，网络问题，镜像不存在。
- `desktop/src/local-stack.ts`：本机服务控制器 `LocalStackController`。
  - 运行阶段：检查 Docker → 准备配置 → 拉取镜像 → 启动 → 等待就绪 → 就绪或失败。状态变化由一个纯函数计算，方便测试。
  - 首次运行时生成 `.env` 里的密钥和身份 token。文件放在 `userData/stack`，权限设为仅本人可读写。
  - 端口：默认 `45180`，被占用时改用一个随机的空闲本机端口，最多重试两次，选定的端口记录下来，下次继续用。
  - 拉取镜像时根据下载字节数估算进度；启动失败时取容器日志的最后 30 行显示给用户。
  - 镜像标签跟随桌面端版本号（`v<版本号>`），开发版本用 `edge`。桌面端升级后，下次启动会自动拉取新镜像并重建容器，数据卷保留。
  - 打开窗口前先快速检查：用 token 访问身份接口，2 秒超时，标签一致就直接打开工作台，不重新部署。这样不会回到“每次启动都要部署一遍”的老问题。
- `desktop/src/local-stack-admin.ts`：首次运行时创建管理员（调用第 2 步第 4 条的命令）。
- `electron-builder.config.cjs`：通过 `extraResources` 把 `deploy/desktop/` 下的 compose 文件和模板打进安装包。

**界面**（`connect.html`、`connect.js`、`contracts.ts`、`preload.ts`）
- 连接页改成三个选项：官方服务（默认）、连接已有服务器、在这台电脑上运行。
- 本机运行面板显示：
  - 检查 Docker 的结果和 Docker Desktop 下载链接
  - 进度条，以及可展开的详细输出
  - 首次运行时设置管理员用户名和密码的表单
- `agoraConnection` 桥接接口升到 v2，增加 `mode` 和 `stack` 两组方法。主进程仍然逐条校验消息是不是来自连接页本身。
- 托盘菜单：“更换服务器地址…”改为“切换连接方式…”；本机模式下增加“停止本机服务”和“查看本机服务日志”。
- 保存时先打开新地址确认能用，再写入配置；写入失败就回退到原来的配置。这是 Rakazo 的做法，比现在“先检查、再保存”稳。

**生命周期**
- 退出桌面端时不停止容器，只中断正在进行的拉取或启动命令。停止服务要从托盘菜单明确操作。

### 第 4 步：文档

- 更新 `docs/hosted-deployment.md`：改成拉取镜像部署，补上安装脚本的用法，以及子网冲突时怎么改配置。
- 新增 `docs/local-mode.md`，说明本机模式：
  - 运行要求：Docker Desktop（Windows 需要 WSL2）、8 GB 以上内存、10 GB 以上可用磁盘。磁盘数字要等第 1 步镜像瘦身后再按实际大小定。
  - 数据存在哪里，怎么备份和恢复，怎么彻底卸载。
- 更新 `desktop/README.md`：三种模式的构建变量，以及测试方法。

## 5. 关键细节

- **两把密钥的编码不同，生成时不能用同一种格式**：
  - `NEWSROOM_CREDENTIAL_ENCRYPTION_KEY`：32 字节随机数的标准 base64。
  - `NEWS_ACTIVITY_ENCRYPTION_KEY`：Fernet 密钥，即 32 字节随机数的 URL 安全 base64。
  - 生成后用服务端同样的校验规则验证一遍，并写进测试。
- **本机模式丢了 `.env` 等于丢数据**：用户保存的模型 key 和研究记录都解不开了。桌面端提供“导出备份”时，要把 `.env` 和数据卷一起打包，并提醒用户妥善保管。
- **桌面端只用 `127.0.0.1`**：服务端要求 `Host` 精确匹配，所以本机模式的地址只用 `127.0.0.1`，不用 `localhost`。
- **本机模式也要填模型 key**：服务端的 key 机制保持不变，所以本机模式下用户同样要在设置里填自己的 key。
- **不碰现有的安全约束**：hosted 配置下 Marker 被禁用、视觉描述被禁用、没有网页初始化入口，这些规则在三种模式下都保持不变。

## 6. 需要你确认的事项

1. **镜像仓库**：
   - GHCR：免费，但国内拉取慢。
   - 阿里云 ACR：国内拉取快。
   - 两边都推：建议这个。桌面端和安装脚本按地区选择拉取源，也可以手动切换。
2. **官方服务的域名**：现在有没有？没有的话，官方服务这个选项先隐藏，等有了域名、在构建时写入后再显示。
3. **Mac（Apple 芯片）本机模式**：要不要第一版就支持？支持的话第 1 步第 2 条必须做；不支持的话先只发布 amd64 镜像，工作量会小一些。
4. **模型连接整合（`codex/e2e-answer-evaluation`）**：
   - 现在 cool-euclid 的服务商只能从目录里选，本机模式下连不了用户自己的 Ollama 这类本地模型。
   - codex 分支的模型连接支持自定义地址，并且有防 SSRF 校验，正好能补上这个缺口，但要解决 12 个文件的冲突。
   - 建议作为下一个独立任务做；三种模式的第一版先沿用目录方式。
5. **本机模式的管理员账号**：
   - 方案 A（推荐）：首次运行时让用户设置用户名和密码，之后正常登录。
   - 方案 B：桌面端自动生成密码、存进系统钥匙串并自动登录。体验更顺，但要多做一套凭据存储，而且安全边界更复杂。

## 7. 验证方式

- **桌面端单元测试**（`desktop/tests`，vitest）覆盖：
  - v1 到 v2 配置迁移，三种模式的地址校验
  - 受管本机地址的信任规则（非受管的本机 HTTP 地址必须被拒绝）
  - 运行阶段的状态计算，Docker 失败原因的分类
  - `.env` 生成，以及两把密钥的编码
  - 端口重试
  - 控制器测试通过注入一个假的 `runDocker` 来实现，不依赖真实的 Docker。这样在 Windows 上也能跑。
- **前端测试**：身份接口在 token 正确时返回内容，token 错误或没设置时返回 404。
- **服务端测试**：本机编排的配置能通过 `load_hosted_settings` 校验；`create-admin --password-stdin` 能正常工作。
- **编排检查**：`docker compose -f deploy/desktop/compose.images.yml config` 和 hosted 的 compose 配置都能通过。
- **桌面端 e2e**（playwright）：
  - 用现有的假服务器 `FakeAgoraServer` 测三种模式的选择和保存。
  - 本机模式用测试模式下注入的假控制器，测完整的进度显示、失败、重试流程。
- **真机验收**（需要安装 Docker Desktop）：
  1. 本地构建镜像，运行桌面端，选择“在这台电脑上运行”。
  2. 依次完成拉取、启动、创建管理员、登录。
  3. 填入模型 key，导入一篇 PDF 并提问。
  4. 退出后重新打开，确认直接进入工作台，并记录启动耗时。
  5. 从托盘停止服务，确认再次打开时会重新启动。
  6. 切换到“连接已有服务器”，连接一套 hosted 部署（用真实域名或 sslip.io 获取证书），确认两边的登录状态互不影响。
