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

- Linux x86_64 或 arm64，建议 2 核 CPU、4 GB 内存、20 GB 磁盘（20 到 100 人同时使用时建议 4 核、8 GB）。服务端不运行任何本地模型，不需要 GPU；api 和 web 镜像解包后各约 500 MB。
- Docker Engine 24 或更新版本，带 `docker compose` 插件。
- 一个指向服务器的域名，且 80 和 443 端口可以从公网访问（证书签发需要）。
  - 只有 IP 没有域名时，可以用通配 DNS 服务，例如 IP 为 `203.0.113.7` 时使用 `203-0-113-7.sslip.io`，无需注册即可解析到该 IP。
  - 必须使用 HTTPS：登录 cookie 带 `Secure` 标记，桌面客户端也只接受 HTTPS 地址。

## 镜像

api 和 web 镜像由 GitHub Actions（`.github/workflows/images.yml`）构建，同时提供 linux/amd64 和 linux/arm64，推送到两个镜像仓库，内容完全相同：

| 仓库 | 地址 | 适用 |
| --- | --- | --- |
| GHCR | `ghcr.io/devin333` | 海外服务器 |
| 阿里云 ACR | `registry.cn-hangzhou.aliyuncs.com/agora-hub` | 中国大陆服务器 |

标签：

- `v<x.y.z>`：正式版本，生产环境请固定使用。
- `edge`：main 分支最新构建。
- `sha-<提交短号>`：每次构建都有，用于精确回到某次提交。

`.env` 里的 `AGORA_IMAGE_REGISTRY` 选仓库，`AGORA_IMAGE_TAG` 选标签。

## 首次部署

推荐用安装脚本。在服务器上新建一个目录，执行：

```bash
mkdir -p agora && cd agora
curl -fsSLO https://raw.githubusercontent.com/Devin333/Agora-Hub/main/deploy/hosted/install.sh
bash install.sh --domain agora.example.com --tag v0.1.2
```

服务器在中国大陆时加 `--mirror-cn`，从阿里云 ACR 拉取镜像。

脚本会：

1. 下载 `compose.yaml`、`Caddyfile`、`.env.example`（只走 HTTPS）。
2. 创建权限为 600 的 `.env`，生成两个主密钥（见下文“主密钥”），写入域名、镜像仓库和标签。已有的 `.env` 和密钥会保留，所以重复执行就是原地升级。
3. 校验 compose 配置，拉取镜像，启动并等待三个服务都变为 `healthy`。

只想先生成文件、检查 `.env` 后再启动时，加 `--prepare-only`；之后执行 `bash install.sh --local`（不再下载文件）。

不用脚本时，手动操作也一样：把 `deploy/hosted` 下的 `compose.yaml`、`Caddyfile`、`.env.example` 放到服务器同一个目录，`cp .env.example .env` 后按注释填写，再执行：

```bash
docker compose pull
docker compose up -d --wait
```

启动后创建第一个管理员。托管版没有自助注册和网页初始化入口，第一个管理员只能在服务器上创建：

```bash
docker compose exec api news accounts create-admin admin
```

命令会提示输入两次密码。密码不会出现在命令行参数或 shell 历史中。需要用脚本创建时，用 `--password-stdin` 从标准输入读入密码：

```bash
printf '%s\n' "$ADMIN_PASSWORD" | docker compose exec -T api news accounts create-admin admin --password-stdin
```

最后用浏览器打开 `https://<你的域名>/paper-agent`，用管理员登录，在“账号设置”里填写模型 key 后即可使用。

### 内部网络

api 和 web 之间是一个固定网段的内部网络（默认 `172.30.10.0/24`），api 只信任 web 的固定地址转来的客户端地址。这个网段和服务器上已有的网络冲突时，在 `.env` 里同时修改 `AGORA_APP_SUBNET`、`AGORA_API_IP`、`AGORA_WEB_IP` 三项，两个地址必须在新网段内。

### 从源码构建

改了代码、需要自己构建镜像时，在仓库的 `deploy/hosted` 目录执行：

```bash
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

服务器访问不了 npm 或 PyPI 官方源时，在 `.env` 里把 `NPM_REGISTRY`、`PIP_INDEX_URL` 改成可用的镜像（`.env.example` 里有示例）。构建出的镜像沿用 `AGORA_IMAGE_REGISTRY`/`AGORA_IMAGE_TAG` 组成的名字，之后普通的 `docker compose up -d` 直接使用它们，不会再拉取。

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
- 可选模型列表打包在 api 镜像里。要换成自己的列表，不必重新构建镜像：把修改后的文件放在 `compose.yaml` 旁边，新建 `compose.override.yaml`（compose 会自动叠加它）：

  ```yaml
  services:
    api:
      environment:
        NEWSROOM_HOSTED_LLM_PROVIDERS_PATH: /etc/agora/hosted-llm-providers.yaml
      volumes:
        - ./hosted-llm-providers.yaml:/etc/agora/hosted-llm-providers.yaml:ro
  ```

  然后执行 `docker compose up -d api`。每个模型的上下文长度必须能容纳一次研究请求，否则 api 拒绝启动。

## 主密钥

托管版有两个密钥，都只生成一次，并在服务器之外另存一份（例如团队的密码管理器）：

- `NEWSROOM_CREDENTIAL_ENCRYPTION_KEY` 加密所有用户的模型 key。丢失或更换后，已保存的 key 都无法解密，所有用户需要重新填写。账号、论文和笔记不受影响。
- `NEWS_ACTIVITY_ENCRYPTION_KEY` 加密每次研究运行记录下来的输入和结果。丢失或更换后，以前的研究运行记录无法再读取，新的研究不受影响。

两个密钥缺失或格式不对时，api 拒绝启动，日志会给出出错的变量名。
- 不要把填好的 `.env` 提交到仓库，也不要发到聊天工具里。

## 论文解析

托管版用内置的 PyMuPDF 文本解析器解析 PDF（`NEWS_RESEARCH_PARSER_BACKENDS=pymupdf`）。高精度的 Marker 解析器运行在独立的 Docker 沙箱里，需要访问 Docker 守护进程；把宿主机的 Docker socket 交给 api 容器等于把宿主机 root 权限交给它，所以托管版不启用 Marker，api 在配置为 marker 时会拒绝启动。

## 升级

用安装脚本时，在部署目录重新执行，带上新的版本号：

```bash
bash install.sh --local --tag v0.1.3
```

手动操作时，把 `.env` 里的 `AGORA_IMAGE_TAG` 改成新版本，然后：

```bash
docker compose pull
docker compose up -d --wait
```

compose 逐个替换容器。api 停止时最多等待 60 秒，让进行中的请求完成。升级期间桌面客户端会显示“无法连接服务器”，并在服务恢复后自动重连；网页用户刷新即可。

回滚：把 `AGORA_IMAGE_TAG` 改回上一个版本，再执行上面两条命令。版本说明里标明数据格式不兼容时，先按“备份与恢复”恢复升级前的备份再回滚。

## 备份与恢复

需要备份的是 `newsroom-data` 卷和 `.env`（两个主密钥）。`caddy-data` 和 `caddy-config` 可以重新生成，不必备份。

备份（会短暂停止 api，保证文件一致）：

```bash
cd agora   # 部署目录
docker compose stop api
docker run --rm -v agora_newsroom-data:/data -v "$PWD":/backup busybox \
  tar czf /backup/newsroom-data-$(date +%Y%m%d).tar.gz -C /data .
docker compose start api
```

恢复到新服务器：先按“首次部署”把同一个 `.env`（同样的两个主密钥）放进部署目录，执行一次 `bash install.sh --local` 或 `docker compose up -d` 创建卷，然后：

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
- 拉取镜像很慢或超时：中国大陆服务器把 `AGORA_IMAGE_REGISTRY` 改为 `registry.cn-hangzhou.aliyuncs.com/agora-hub`（或重新执行 `bash install.sh --local --mirror-cn`）。
- 启动时提示网段冲突（`Pool overlaps with other one on this address space`）：按“内部网络”修改三项网段配置。
- 登录提示过于频繁：同一客户端地址每分钟最多尝试 `NEWSROOM_HOSTED_LOGIN_RATE_LIMIT_PER_MINUTE` 次，等一分钟再试。
