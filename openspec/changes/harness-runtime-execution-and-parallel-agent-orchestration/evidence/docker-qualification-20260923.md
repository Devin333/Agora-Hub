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
```

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

## 资格边界

这份证据支持 Docker provider 的本地 capability admission、filesystem/environment/network/process-tree/timeout 运行能力。它不证明多机部署、生产镜像供应链、动态 Research golden parity、并行发布 gate 或 rollback rehearsal；这些仍保持未完成，不能勾选对应 release task。
