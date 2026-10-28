# 托管版部署（服务器）

托管版把论文工作台部署在一台服务器上，用户用浏览器或桌面客户端打开同一个地址即可使用，本机不需要安装 Python、Node 或数据库。账号由管理员创建，每个用户在“账号设置”里填写自己的模型 API key，调用费用计入用户自己的账号。

## 架构

```
浏览器 / 桌面客户端
        │ HTTPS（443）
        ▼
     caddy  ── 自动申请和续期证书，只转发到 web
        │
        ▼
      web   ── Next 前端与 BFF：会话 cookie、同源校验、受控的 API 代理
        │ 内部网络（不对外）
        ▼
      api   ── FastAPI：账号、研究、论文解析、检索与问答
```

- 只有 caddy 对外开放 80/443 端口。web 只和 caddy 在同一网络，api 只和 web 在同一网络，api 的端口不对外开放。
- 会话保存在 httpOnly cookie 中，浏览器拿不到会话令牌。web 把会话转成内部请求头交给 api。
- caddy 用自己接到的连接地址覆盖客户端发来的 `X-Forwarded-For`。web 把这条地址链转给 api，api 只信任 web 容器的地址（`NEWSROOM_TRUSTED_PROXY_CIDRS`），据此按真实客户端地址限制登录频率并记录会话来源。
- 所有数据（账号、会话、加密后的模型 key、每个账号的论文、笔记、记忆和运行记录）都在 `newsroom-data` 卷中。账号之间互不可见。

## 服务器要求

- Linux x86_64，建议 4 核 CPU、8 GB 内存、40 GB 磁盘。重排模型在 CPU 上运行，不需要 GPU。
- Docker Engine 24 或更新版本，带 `docker compose` 插件。
- 一个指向服务器的域名，且 80 和 443 端口可以从公网访问（证书签发需要）。
  - 只有 IP 没有域名时，可以用通配 DNS 服务，例如 IP 为 `203.0.113.7` 时使用 `203-0-113-7.sslip.io`，无需注册即可解析到该 IP。
  - 必须使用 HTTPS：登录 cookie 带 `Secure` 标记，桌面客户端也只接受 HTTPS 地址。

## 首次部署

1. 把仓库放到服务器上，进入部署目录：

   ```bash
   cd deploy/hosted
   cp .env.example .env
   ```

2. 编辑 `.env`：
   - `NEWSROOM_DOMAIN`：用户访问的域名，不带 `https://`。
   - `NEWSROOM_CREDENTIAL_ENCRYPTION_KEY`：用 `openssl rand -base64 32` 生成，见下文“主密钥”。
   - `NEWS_ACTIVITY_ENCRYPTION_KEY`：用 `openssl rand -base64 32 | tr '+/' '-_'` 生成，见下文“主密钥”。
   - 服务器在中国大陆时，把 `HF_ENDPOINT` 改为 `https://hf-mirror.com`，否则首次启动下载重排模型可能很慢或失败。
   - 服务器访问不了 npm、PyPI 或 PyTorch 官方源时，在 `.env` 里把 `NPM_REGISTRY`、`PIP_INDEX_URL`、`TORCH_INDEX_URL` 改成可用的镜像（`.env.example` 里有示例），否则构建镜像时下载依赖会失败。

3. 构建并启动：

   ```bash
   docker compose up -d --build
   ```

   首次构建会下载 Python 和 Node 依赖，需要十几分钟。api 首次启动时会下载约 2 GB 的重排模型（`BAAI/bge-reranker-v2-m3`）到 `hf-cache` 卷，之后重启不再下载。

4. 确认三个服务都是 `healthy`：

   ```bash
   docker compose ps
   ```

5. 创建第一个管理员。托管版没有自助注册和网页初始化入口，第一个管理员只能在服务器上创建：

   ```bash
   docker compose exec api news accounts create-admin admin
   ```

   命令会提示输入两次密码。密码不会出现在命令行参数或 shell 历史中。

6. 用浏览器打开 `https://<你的域名>/paper-agent`，用管理员登录，在“账号设置”里填写模型 key 后即可使用。

## 账号管理

管理员登录后可以在工作台“设置”的账号管理中创建、停用、启用账号和重置密码，也可以在服务器上用命令完成：

```bash
docker compose exec api news accounts list
docker compose exec api news accounts create-user alice --generate-password
docker compose exec api news accounts disable alice
docker compose exec api news accounts enable alice
docker compose exec api news accounts reset-password alice --generate-password
```

`--generate-password` 会生成一次性密码并只显示一次，请通过安全渠道交给用户，由用户登录后自行修改。停用账号和重置密码会立即结束该账号的所有会话。

## 模型 key 与可选模型

- 用户只能从 `configs/hosted-llm-providers.yaml` 列出的服务商和模型中选择，不能填写自定义地址，因此 key 只会发往列表中的地址。
- key 用主密钥加密后保存，网页和接口都不会再返回明文。
- 修改可选模型列表后需要重新构建 api 镜像（`docker compose up -d --build api`）。每个模型的上下文长度必须能容纳一次研究请求，否则 api 拒绝启动。

## 主密钥

托管版有两个密钥，都只生成一次，并在服务器之外另存一份（例如团队的密码管理器）：

- `NEWSROOM_CREDENTIAL_ENCRYPTION_KEY` 加密所有用户的模型 key。丢失或更换后，已保存的 key 都无法解密，所有用户需要重新填写。账号、论文和笔记不受影响。
- `NEWS_ACTIVITY_ENCRYPTION_KEY` 加密每次研究运行记录下来的输入和结果。丢失或更换后，以前的研究运行记录无法再读取，新的研究不受影响。

两个密钥缺失或格式不对时，api 拒绝启动，日志会给出出错的变量名。
- 不要把填好的 `.env` 提交到仓库，也不要发到聊天工具里。

## 论文解析

托管版用内置的 PyMuPDF 文本解析器解析 PDF（`NEWS_RESEARCH_PARSER_BACKENDS=pymupdf`）。高精度的 Marker 解析器运行在独立的 Docker 沙箱里，需要访问 Docker 守护进程；把宿主机的 Docker socket 交给 api 容器等于把宿主机 root 权限交给它，所以托管版不启用 Marker，api 在配置为 marker 时会拒绝启动。

## 升级

```bash
git pull
cd deploy/hosted
docker compose up -d --build
```

compose 会先构建新镜像，再逐个替换容器。api 停止时最多等待 60 秒，让进行中的请求完成。升级期间桌面客户端会显示“无法连接服务器”，并在服务恢复后自动重连；网页用户刷新即可。

需要回滚时，升级前在 `.env` 里把 `AGORA_IMAGE_TAG` 设为本次版本名（例如 `2026-10-27`），旧镜像会保留在本机；回滚时改回旧的标签再执行 `docker compose up -d`（不带 `--build`）。

## 备份与恢复

需要备份的是 `newsroom-data` 卷和 `.env`（两个主密钥）。`hf-cache` 和 `caddy-data` 可以重新生成，不必备份。

备份（会短暂停止 api，保证文件一致）：

```bash
cd deploy/hosted
docker compose stop api
docker run --rm -v agora_newsroom-data:/data -v "$PWD":/backup busybox \
  tar czf /backup/newsroom-data-$(date +%Y%m%d).tar.gz -C /data .
docker compose start api
```

恢复到新服务器：先按“首次部署”准备好同一个 `.env`（同样的两个主密钥），执行一次 `docker compose up -d` 创建卷，然后：

```bash
docker compose stop api
docker run --rm -v agora_newsroom-data:/data -v "$PWD":/backup busybox \
  sh -c "rm -rf /data/* && tar xzf /backup/newsroom-data-20261027.tar.gz -C /data"
docker compose start api
```

## 桌面客户端

为用户构建写入了服务器地址的安装包（在开发机上执行）：

```powershell
$env:AGORA_DESKTOP_SERVER_URL = 'https://agora.example.com'
npm --prefix desktop run dist
```

用户安装后直接打开工作台。没有写入地址的安装包首次启动会询问地址。客户端的行为见 `desktop/README.md`。

## 排查

- `docker compose logs -f api`、`docker compose logs -f web`、`docker compose logs -f caddy` 查看日志。
- api 拒绝启动时，日志第一行会给出出错的变量名，例如缺少主密钥或活动加密密钥、模型列表不合法或解析器配置成了 marker。
- 证书申请失败：确认域名已解析到服务器公网 IP、80 和 443 端口未被其他程序占用且防火墙已放行。
- 登录提示过于频繁：同一客户端地址每分钟最多尝试 `NEWSROOM_HOSTED_LOGIN_RATE_LIMIT_PER_MINUTE` 次，等一分钟再试。
