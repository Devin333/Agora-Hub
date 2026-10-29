# 在这台电脑上运行（本机模式）

桌面客户端有三种连接方式：官方服务、连接已有服务器、在这台电脑上运行。本机模式用 Docker 在用户自己的电脑上运行和托管版完全相同的 api、web 两个镜像，论文、笔记、记忆、账号和模型 key 都只保存在这台电脑上。服务器部署见 `docs/hosted-deployment.md`，客户端行为见 `desktop/README.md`。

## 电脑要求

| 项目 | 要求 |
| --- | --- |
| 系统 | Windows 10/11 64 位；macOS（Apple 芯片，第一版只提供 arm64 安装包） |
| Docker | Windows、macOS 安装 Docker Desktop（Windows 需启用 WSL 2）；也可以用 OrbStack 等兼容 `docker compose` 的环境 |
| 内存 | 建议 8 GB 以上。api 容器上限 2 GB，web 容器上限 1 GB |
| 磁盘 | 两个镜像解压后各约 500 MB，加上论文资料，建议预留 5 GB 以上 |
| 网络 | 首次启动和升级时需要下载镜像；之后只有调用模型时需要联网 |

当前 Windows 用户需要有使用 Docker 的权限（Docker Desktop 安装时会加入 `docker-users` 组，加入后要重新登录系统）。

## 首次启动

1. 在连接页选择“在这台电脑上运行”，选择镜像下载源：
   - GitHub 镜像仓库（海外网络）：`ghcr.io/devin333`
   - 阿里云镜像仓库（中国大陆网络）：`registry.cn-hangzhou.aliyuncs.com/agora-hub`
2. 客户端依次检查 Docker、准备配置、下载镜像、启动服务、确认服务可用，连接页显示每一步和下载进度。下载可以随时取消，已下载完成的镜像层下次不会重新下载。
3. 服务第一次启动时还没有账号，连接页会要求创建管理员（用户名 3 到 64 个字符，密码至少 8 个字符）。密码通过标准输入交给容器里的 `news accounts create-admin`，不会出现在命令参数或日志里。
4. 创建后自动打开登录页，用刚才的账号登录，在“账号设置”里填写自己的模型 API key。

服务只监听 `127.0.0.1`，同一网络的其他设备访问不到。默认端口是 45180，被其他程序占用时客户端会自动换一个空闲端口并记住。

## 日常使用

- 容器设置了 `restart: unless-stopped`，退出客户端后服务继续运行，Docker 启动时也会自动恢复。下次打开客户端时会先确认端口上是自己的服务（`/.well-known/agora-desktop-stack`，需要只有本机知道的令牌），确认后直接打开工作台，通常 1 到 2 秒。
- 托盘菜单：
  - “切换连接方式…”：改用服务器或官方服务，本机服务不受影响，之后可以再切回来。
  - “停止本机服务”：停止容器，释放内存。数据不受影响，在连接页点“立即重试”重新启动。
  - “查看本机服务日志”：把最近的容器日志（已去掉密钥和令牌）写入 `logs/local-stack.log` 并打开。
- 升级：客户端每个版本对应同版本号的服务镜像（`v<版本号>`）。安装新版客户端后第一次打开会下载新镜像并替换容器，数据卷保持不变。

## 数据保存在哪里

| 内容 | 位置 |
| --- | --- |
| 配置与密钥 | 客户端数据目录下的 `stack/.env`（Windows：`%APPDATA%\Agora Paper Agent\stack`；macOS：`~/Library/Application Support/Agora Paper Agent/stack`） |
| 账号、加密后的模型 key、论文、笔记、记忆、运行记录 | Docker 数据卷 `agora-desktop_newsroom-data` |

`.env` 里有两个主密钥：`NEWSROOM_CREDENTIAL_ENCRYPTION_KEY` 加密模型 key，`NEWS_ACTIVITY_ENCRYPTION_KEY` 加密运行记录。客户端只在第一次生成，之后每次启动都保留原值。丢失或替换 `.env` 后，数据卷里已保存的模型 key 和运行记录将无法解密，所以备份时 `.env` 和数据卷必须一起保存。

## 备份与恢复

备份：先在托盘点“停止本机服务”，保证文件一致，然后在命令行执行（Windows 用 PowerShell，命令相同）：

```bash
docker run --rm -v agora-desktop_newsroom-data:/data -v "$PWD":/backup busybox tar czf /backup/agora-local-data.tar.gz -C /data .
```

再把客户端数据目录下的 `stack/.env` 复制到同一个地方。备份完成后在连接页点“立即重试”重新启动。

恢复到另一台电脑（在第一次选择本机模式之前进行）：

1. 安装客户端并打开一次后退出，把备份的 `.env` 放到新电脑的客户端数据目录 `stack/.env`。
2. 创建数据卷并解压备份：

   ```bash
   docker volume create --label com.docker.compose.project=agora-desktop --label com.docker.compose.volume=newsroom-data agora-desktop_newsroom-data
   docker run --rm -v agora-desktop_newsroom-data:/data -v "$PWD":/backup busybox tar xzf /backup/agora-local-data.tar.gz -C /data
   ```

3. 打开客户端，选择“在这台电脑上运行”。客户端沿用 `.env` 里的密钥，发现已有账号后直接打开登录页，不会再要求创建管理员。

## 卸载

卸载客户端只删除程序本身，容器、数据卷和客户端数据目录都会保留，重新安装后可以继续使用。要彻底删除本机服务和全部数据：

```bash
docker compose --project-name agora-desktop down --volumes
```

然后删除客户端数据目录。执行前请确认已经备份，删除后无法恢复。

## 排查

| 连接页提示 | 处理 |
| --- | --- |
| 这台电脑上没有可用的 Docker | 安装 Docker Desktop，启动后点“立即重试”。连接页提供下载链接 |
| Docker 没有运行 | 打开 Docker Desktop，启动完成后客户端会自动继续 |
| 当前用户没有权限使用 Docker | 把当前用户加入 `docker-users`（Windows）或 `docker` 组，重新登录系统 |
| 镜像仓库里没有这个版本的服务镜像 | 换一个镜像下载源，或确认客户端版本对应的镜像已经发布 |
| 镜像下载失败 / 下载很慢 | 中国大陆网络选阿里云镜像仓库；检查 Docker Desktop 的代理设置 |
| 磁盘空间不足 | 清理磁盘，或在 Docker Desktop 里清理不再使用的镜像 |
| 本机端口被其他程序占用 | 关闭占用端口的程序后重试 |
| 本机服务启动失败 / 超时 | 展开连接页的“详细输出”，或用托盘“查看本机服务日志”查看原因 |
