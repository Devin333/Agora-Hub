# Runtime Qualification Evidence

日期：2026-09-22

## 已验证的本地范围

运行时、AgentLoop、TaskPlan、dynamic Research、接口 composition 与 architecture 回归使用 `E:/Anaconda3/python.exe`（Python 3.12.7）执行。结果：

```text
python -m pytest tests/framework/harness/task_plan tests/framework/harness/agent_loop tests/backend/research/integration/test_dynamic_paper_analysis_task_plan.py tests/interfaces/composition/test_agent_loop_orchestration_composition.py tests/architecture -q
1394 passed, 6 warnings
```

此前的 execution-environment / child-supervisor / tool-runtime / runtime composition 范围为 `171 passed, 2 skipped`；两个跳过项是 Windows 无法创建符号链接的测试环境限制。Research claim review 首片另有 [stage1-contracts.md](../../research-evidence-review-resolution/evidence/stage1-contracts.md)。

静态 production-caller 扫描只找到 `subprocess.Popen`/`subprocess.run` 位于 `infrastructure/execution_environment/docker.py`；已定位的业务入口使用 `RuntimeExecutionComposition`、`ChildAgentSupervisor`、`SubAgentRuntime` 和 `ToolExecutor`。`backend/research/document/pdf_compiler.py` 的注释明确拒绝 host subprocess fallback。这份文本扫描不证明全部动态调用链、隔离策略或生产恢复接线均已验收。

## 部署能力结果

```text
build_process_execution_composition(required_provider_ids=("docker",)).diagnostics()
status: blocked
unavailable_providers: ["docker"]
provider_capabilities.docker.available: false
```

该次探测时 Docker daemon 不可用；这不能证明 CLI 未安装。2026-09-23 启动 Docker Desktop 后 engine 已响应，本地 provider 证据见 [docker-qualification-20260923.md](docker-qualification-20260923.md)。provider 可用不等于并行发布资格通过，任务 2.6、7.5 和 7.6 仍需要完整部署及回滚证据。

## Smoke 后续结果

2026-09-22 启动的 `E:/Anaconda3/python.exe -m scripts.dev smoke` 已终止，退出码为 0：`3664 passed, 23 deselected, 32 warnings in 3740.65s`。后续离线 AgentLoop fixture 返回 `status=succeeded`、`network_calls=0`，source validation 返回 `is_valid=true`、`error_count=0`、`warning_count=0`。

这次结果覆盖 Docker provider、SQLite lifecycle 和输入合同后续变更之前的代码，不能作为这些后续提交的 smoke 验证。它也不替代任务 7.2 中独立的完整 `scripts.dev test`。

## 未完成

本文件不勾选任何发布任务。进程重启/取消不确定性的真实外部 provider 场景、production caller 完整审计、完整 test、部署 capability/rollback、Research golden parity 和 allowlisted dynamic Research release 仍需后续证据。

## 2026-09-23 本轮提交前 smoke

本轮基于 HEAD `63ae01b9feb7cfc24863c4fca7fd296de1044f9b`，覆盖本地 supervisor cancellation identity、Docker cleanup 和 spawn recovery fixture 修复后的稳定生产代码。精确变更范围和来源基线见 [merged-traceability-20260923.md](merged-traceability-20260923.md)。

```text
E:/Anaconda3/python.exe -m scripts.dev smoke
compileall: exit 0
3676 passed, 23 deselected, 32 warnings in 3964.62s (1:06:04)
offline AgentLoop: status=succeeded, network_calls=0
sources validate: is_valid=true, error_count=0, warning_count=0
complete command exit code: 0
```

23 个 deselected 为仓库默认排除的 live Research 用例；32 条 warning 为 protobuf 与 FastAPI 的 deprecation warnings。离线 AgentLoop run 为 `test-agent-loop-428036d54c3c487bad8184480e46b4c0`，manifest checksum 为 `sha256:aa47f6cfb26178c4a9381daf412cece66e07b6c16e804410574afc23757be874`。

Docker integration 不在该 smoke 的测试选择中，另行显式验证为 `42 passed, 2 skipped`，其中六个真实 Docker 场景全部通过；详见 Docker evidence。取消身份范围 `131 passed`，恢复编排范围 `96 passed`。`openspec validate --all --strict` 为 `559 passed, 0 failed`，本轮变更的单项 strict validation 与 diff check 也通过。

本轮 smoke 已取得完整终态；它仍不替代 `python -m scripts.dev test` 的全仓库测试，也不证明独立进程恢复、动态 Research 生产发布或回滚资格。任务 7.2 保持未完成。
