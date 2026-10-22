## Why

用户确认 Research 的目标回答模型为 `gpt-5.6-sol`，但 repo/dev 环境变量不代表 Electron 桌面运行时已切换。Electron 配置由持久化 owner 投影；必须通过桌面配置 preview/apply 与 release 工作流实际设置并验证，不能以仓库 `.env` 或 `.env.example` 宣称桌面切换完成。

当前 Electron runtime root 的公开状态为 `revision=0`、`llm=null`，credential inspection 显示 `configured=false`，说明桌面从未完成模型或凭证配置。针对 `gpt-5.6-sol` 与 `https://deedee.tech/v1` 的 public config preview 已成功，但 config apply 以 `desktop_runtime_config_release_required` fail closed；旧 `active-release.json` 未通过当前验证，且失败没有写入配置。进一步确认最初 admission drift 来自继承的 repo `NEWS_MODELS_CONFIG` 污染 release identity；代码现已将 admission 放入 desktop owned environment。该修复后，active release 验证暴露独立的 `desktop_release_dependency_drift`：manifest 比实际安装环境多声明 52 个 Python packages，必须重建并重装 release，不能安装补丁依赖、篡改 manifest 或绕过验证。实际 release 重建重装、凭证配置、激活和运行验证仍待完成。 网关模型目录包含该 model id 只构成 `listed` 证据；通过 framework client route 发起的 plain-text 单次探针（`max_tokens=32`、`timeout=45`、`max_attempts=1`、提示 `Reply exactly OK`）在 45 秒超时，不能宣称 live 请求成功。该结果与通过的本地测试相互独立。

## What Changes

- 为 repo/dev Research 配置提供专用 OpenAI-compatible route，部署 model id 固定为 `gpt-5.6-sol`，凭证仅通过环境变量引用，不提交 secret。
- `.env.example` 只描述 repo/dev Research route，不将其称作 Electron 桌面配置。
- Electron owner 必须通过 config preview/apply 设置目标模型，并保留固定 credential ref；不得绕过 release 流程。
- 修复 `config_apply` admission 使用父进程/repo 环境进行 release identity 验证的问题，确保 admission 与 apply 使用相同的 desktop owned environment。
- 记录 public preview 已成功、初始 apply 被 release gate 拒绝且零写入；修复环境污染后仍由真实 installed release dependency drift fail closed，不将其标记为实际桌面切换完成。
- 保持通用 writer、评测生成模型、judge 模型和其他 provider route 不变。
- 不授予未经资格验证的 structured-output 能力；不改拒答前缀协议。

## Capabilities

### New Capabilities
- `research-desktop-answer-model`: 为 repo/dev Research route 明确配置 `gpt-5.6-sol`，并要求 Electron owner 配置通过受控 preview/apply、credential provisioning 与 release 流程完成。

### Modified Capabilities
- None.

## Impact

- `configs/models.yaml`：新增 Research 专用部署和 route。
- `.env.example`：记录 repo/dev Research route，不代表 Electron owner 状态。
- `tests/interfaces/composition/`：覆盖 repo/dev route 解析、模型身份和配置隔离。
- Electron owner 的实际配置、凭证和激活需另经受控 preview/apply 与 release 流程完成。
- `openspec/changes/research-desktop-answer-model-gpt-5-6-sol/`：记录规范、设计、已知 gate 结果和验收任务。
