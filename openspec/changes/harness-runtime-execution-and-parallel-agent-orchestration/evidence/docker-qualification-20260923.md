# Docker Provider Qualification Evidence

日期：2026-09-23

## 环境

本机 Docker Desktop 4.72.0 / Engine 29.4.2（Linux amd64）已启动。运行时 composition 的真实诊断为：

```text
status: ready
composition_id: newsroom-runtime
required_providers: [docker]
unavailable_providers: []
provider_capabilities.docker.available: true
provider_capabilities.docker.enforces_filesystem_roots: true
provider_capabilities.docker.enforces_network_deny: true
provider_capabilities.docker.isolates_environment: true
provider_capabilities.docker.controls_process_tree: true
provider_capabilities.docker.enforces_process_limits: true
provider_capabilities.docker.confirms_termination: true
provider_capabilities.docker.enforces_network_allowlist: false
provider_capabilities.docker.enforces_child_process_allowlist: false
provider_capabilities.docker.enforces_resource_limits: false
provider_capabilities.docker.enforces_memory_limits: true
provider_capabilities.docker.enforces_cpu_limits: false
provider_capabilities.docker.supports_secret_handles: false
```

本轮复测的 manifest fingerprint 为 `sha256:ab7c276f990606f04d90e449961f7ff8fb06569853f7529a9c089f8b07014dc3`。`ready` 表示必需 provider 可用，不代表支持全部 profile 或资源限制；上述不支持的 capability 仍须在具体请求准入时拒绝。

## 真实 provider 验证

使用 `NEWSROOM_DOCKER_INTEGRATION=1` 显式开启真实 provider 测试，并使用本机已存在的 `redis:7-alpine` image ID（测试通过 image ID 固定执行，不依赖仓库拉取）：

```text
E:/Anaconda3/python.exe -m pytest tests/infrastructure/execution_environment/test_docker_provider_integration.py tests/framework/execution_environment -q
37 passed, 2 skipped
```

新增的五个场景全部通过：

- 只读根拒绝写入，可写根允许复制，容器退出被确认；
- 主机环境变量不会进入容器，只注入显式 allowlisted 环境变量；
- `--network none` 阻止外部连接；
- 超时后执行 stop/wait，终止被确认且容器被清理；
- provider 不支持 network allowlist 时在 launch 前 fail closed，未创建容器。

测试完成后 `docker ps -a --filter name=newsroom-exec-` 没有残留 qualification 容器。

## 修复的 provider 缺陷

真实验证发现并修复了三个实际命令兼容问题：可写 bind mount 不应使用无效的裸 `rw` 字段；BusyBox `env` 不接受 GNU `--` 分隔符；`docker logs` 不支持 `--stdout/--stderr` selector。修复后保留正常退出状态，只有无法确认终止或日志/daemon 协议异常时才返回 `INDETERMINATE`。

## 清理与资格断言复核

后续复核补充了两个清理失败回归：`docker rm` 返回非零或命令异常时，receipt 必须为 `INDETERMINATE`、`termination_confirmed=False`，不能保留成功声明。正常清理使用 `docker rm -f -v`，删除该次 execution 容器及其匿名卷。

真实 integration 为每次 execution 使用唯一标识，并新增 Redis image `/data` 匿名卷在容器退出后被删除的场景。网络用例检查启动 marker、退出码和 BusyBox 的明确 `network unreachable` 诊断，排除命令缺失造成的假通过。显式启用 integration 时 daemon 或镜像不可用会失败，不会跳过。

本轮 daemon 最初未启动，显式运行产生 6 个 setup errors；启动 Docker Desktop 后，测试设置中的网络错误文本和空 argv 参数问题已修正。最终命令及结果：

```text
NEWSROOM_DOCKER_INTEGRATION=1
E:/Anaconda3/python.exe -m pytest tests/infrastructure/execution_environment/test_docker_provider_integration.py tests/infrastructure/execution_environment/test_docker_provider_cleanup.py tests/framework/execution_environment -q
42 passed, 2 skipped in 10.60s
exit code: 0
```

六个真实 Docker 场景全部通过；两个 skip 来自现有 Windows 平台相关 filesystem contract。匿名卷测试只清理本次唯一容器及从该容器 inspect 得到的精确 volume，不扫描清理其他运行资源。

## 资格边界

这份证据支持 Docker provider 的本地 capability admission、filesystem/environment/network/process-tree/timeout 运行能力。它不证明多机部署、生产镜像供应链、动态 Research golden parity、并行发布 gate 或 rollback rehearsal；这些仍保持未完成，不能勾选对应 release task。
