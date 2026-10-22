## Context

repo/dev Research composition 由 `ResearchRuntimeSettings` 加载，再由 `_resolve_research_llm_deployment` 校验 route deployment 与环境配置。此路径及 `.env.example` 仅控制 repo/dev 配置。Electron 桌面配置的 owner 是 `scripts/local_desktop/runtime_config.py` 中的持久化 owner 投影；`resolve_desktop_app_config` 会清除继承的 Research env，因此仓库 `.env` 不能切换 Electron 的实际模型。`configs/models.yaml` 的 `writer-primary` 被其他工作流复用，不能改写该共享 route。

Electron runtime root 的公开检查结果是 `revision=0`、`llm=null`，credential inspection 为 `configured=false`。public config preview 对 `gpt-5.6-sol` 与 `https://deedee.tech/v1` 成功；config apply 随后以 `desktop_runtime_config_release_required` fail closed。虽然 `active-release.json` 存在，但其旧 release 未通过当前验证，因此没有任何桌面配置写入。根因定位表明 `config_apply` 的 pre-stop admission 原先位于 `controlled_process_environment(self.config)` 之外，导致 release identity 读取继承的 repo `NEWS_MODELS_CONFIG`；真正 owned environment 下 expected 与 actual identity 一致。代码修复后 admission 与 apply 使用同一 owned environment，同时仍保持 admission 在 stop 之前。随后 active release 验证暴露独立的 `desktop_release_dependency_drift`：manifest 比实际 installed Python environment 多声明 52 个 packages（包括 `httpx`、`python-dotenv`、`numpy`、`qdrant-client`、`sentence-transformers`、`pytest` 等），实际环境没有对应新增项。该 release 必须重建并重装，不得安装补丁依赖、篡改 manifest 或绕过 gate。所有拒绝均是安全 fail-closed，不是桌面切换完成。 另外，网关模型目录仅证明 `gpt-5.6-sol` 被列出。framework client route 的 plain-text 单次模型探针使用 `max_tokens=32`、`timeout=45`、`max_attempts=1` 和提示 `Reply exactly OK`，结果为 45 秒超时；因此没有 live completion 成功证据，且此远端结果不能由本地 pytest、compile、strict 或 smoke 代替。

## Goals / Non-Goals

**Goals**
- 将 repo/dev Research 专用 route 配置为 `gpt-5.6-sol`。
- Electron 目标模型必须通过 config preview/apply 写入实际 desktop owner，并经原有 release 流程；保留固定 credential ref。
- Electron 实际激活前完成 credential provisioning，并验证应用后的运行时模型。
- 不把 secret 写入仓库、诊断或输出。
- 通过专用 route 避免影响通用 writer、评测生成和 judge。

**Non-Goals**
- `.env` 或 `.env.example` 不能作为 Electron 已切换的证据。
- 本次文档/测试命名修正不修复 release root cause、不执行成功 apply，也不声称实际桌面切换已完成。
- 不更改 `NEWS_LLM_*` 通用 route 或评测/judge model。
- 不宣称/启用未完成资格评测的 structured-output 能力。
- 不更改回答拒答协议、LLM 温度、prompt、token budget 或 Harness routing。

## Decisions

### D1：使用专用 Research deployment 与 route

新增 `research-desktop-answer` route，绑定独立 `research-desktop-answer` model group/deployment，不修改共享 `writer-primary`。model id 固定为 `gpt-5.6-sol`；base URL 通过配置引用，credential 只通过配置引用使用，凭证值不进入 config、仓库或诊断。

### D2：区分 repo/dev 与 Electron owner

`.env.example` 中的 `NEWS_RESEARCH_LLM_*` 仅说明 repo/dev Research route。Electron 桌面端必须由持久化 owner 投影管理，使用 config preview/apply 设置实际模型；不得依赖继承的 repo `.env`。任何 Electron 配置变更必须保留固定 credential ref，且不得绕过既有 release 流程。

### D3：维持 fail-closed structured-output 能力

模型目录可见不证明结构化输出合同、上下文上限和重试语义通过项目资格门。新 deployment 不声明 structured-output capability；Research answer 的显式拒答前缀协议继续工作。不得以模型名称推断并开放 JSON schema。

### D4：Admission 必须使用 desktop owned environment

`RuntimeConfigManager.admit` 必须在 `controlled_process_environment(self.config)` 内执行，与后续 apply 的环境所有权一致。这样父进程或 repo `.env` 继承的 `NEWS_MODELS_CONFIG` 不会污染 release identity。admission 仍必须先于 stop；错误 confirmation token、release gate 或其他 admission 失败不得停止 runtime，也不得写入配置。

### D5：验证与完成状态

repo/dev composition 测试校验专用 route 的 model/base/key-env 匹配及 `writer-primary` 隔离。Electron public preview 已证明候选 model/base 通过 preview，但 apply 被当前 release gate 拒绝，持久化 owner 仍为 `revision=0`、`llm=null`，credential 仍未配置。owned-environment admission 污染已修复并有回归测试。模型目录状态为 `listed`，但 plain-text live probe 单次 45 秒超时，不能标记为 live 成功。当前 installed release dependency drift、credential `configured=false` 和尚无成功 live completion 证据必须分别追踪。只有 release 重建重装、credential provision、成功 apply 和运行时验证全部完成后，才能记录桌面切换完成。

## Risks / Trade-offs

- [Electron owner 未应用目标模型] → 当前已确认零写入；修复 release 验证根因后重新 preview/apply，并验证持久化 revision 与运行时模型。不得用 repo `.env` 替代。
- [credential 尚未配置] → 在实际激活前通过受控 credential 流程配置，owner 中仅保留固定 credential ref，诊断不输出 secret。
- [为通过 gate 而绕过 release] → 禁止绕过；owned-environment admission 污染通过代码修复解决，`desktop_release_dependency_drift` 必须通过重建并重装 verified release 解决，不得补装依赖或修改 manifest。
- [结构化 Research worker 需要尚未核验的模型能力] → 不声明或推断 structured-output capability，保持 fail-closed。
- [将目录或本地测试误当 live 成功] → 模型目录只记录 `listed`；45 秒单次探针超时单独记录为远端未成功，本地测试仅证明配置和控制流合同。

## Migration Plan

1. repo/dev 使用专用 `research-desktop-answer` route；`.env.example` 仅描述该范围。
2. 记录 public preview 对 `gpt-5.6-sol` 与 `https://deedee.tech/v1` 成功，以及 apply 因 `desktop_runtime_config_release_required` 零写入失败。
3. 运行定向 composition 测试和 OpenSpec strict validation；已有 targeted 100 passed、compile、strict 和 smoke exit 0 可记录为仓库检查，不作为 Electron apply 证据。
4. 已修复 pre-stop admission 的 owned-environment 边界，并通过定向 desktop tests 验证环境隔离、`admit -> stop -> apply` 顺序及 admission fail-closed。
5. 记录模型目录为 `listed`，并记录 framework client plain-text 单次探针在 45 秒超时；不得把本地测试或目录响应表述为 live 成功。
6. 后续重建并重装 verified installed release 以消除真实 dependency drift，完成 credential provisioning，再通过 Electron config preview/apply 和既有 release 流程；验证持久化 owner 与实际桌面运行时模型后，方可记录桌面切换完成。
7. 回滚也须通过 owner preview/apply 和既有 release 路径执行，不以 repo `.env` 变更代替。

## Open Questions

- installed release 已确认存在 `desktop_release_dependency_drift`，需由主流程重建并重装；本代码修复不操作 installed resources。
- framework client 的 plain-text live probe 已在 45 秒单次超时；后续成功证据仍待获取。
- Electron credential provisioning、成功 apply、实际激活和运行验证尚未完成；在证据产生前，不得宣称 Electron 已切换至 `gpt-5.6-sol`。
