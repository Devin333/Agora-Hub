## Context

repo/dev Research composition 由 `ResearchRuntimeSettings` 加载，再由 `_resolve_research_llm_deployment` 校验 route deployment 与环境配置。此路径及 `.env.example` 仅控制 repo/dev 配置。Electron 桌面配置的 owner 是 `scripts/local_desktop/runtime_config.py` 中的持久化 owner 投影；`resolve_desktop_app_config` 会清除继承的 Research env，因此仓库 `.env` 不能切换 Electron 的实际模型。`configs/models.yaml` 的 `writer-primary` 被其他工作流复用，不能改写该共享 route。

Electron runtime root 的公开检查结果是 `revision=0`、`llm=null`，credential inspection 为 `configured=false`。public config preview 对 `gpt-5.6-sol` 与 `https://deedee.tech/v1` 成功；config apply 随后以 `desktop_runtime_config_release_required` fail closed。虽然 `active-release.json` 存在，但其旧 release 未通过当前验证，因此没有任何桌面配置写入。根因定位表明 `config_apply` 的 pre-stop admission 原先位于 `controlled_process_environment(self.config)` 之外，导致 release identity 读取继承的 repo `NEWS_MODELS_CONFIG`；真正 owned environment 下 expected 与 actual identity 一致。代码修复后 admission 与 apply 使用同一 owned environment，同时仍保持 admission 在 stop 之前。随后 active release 验证暴露独立的 `desktop_release_dependency_drift`：manifest 比实际 installed Python environment 多声明 52 个 packages（包括 `httpx`、`python-dotenv`、`numpy`、`qdrant-client`、`sentence-transformers`、`pytest` 等），实际环境没有对应新增项。该 release 必须重建并重装，不得安装补丁依赖、篡改 manifest 或绕过 gate。所有拒绝均是安全 fail-closed，不是桌面切换完成。 首次检查时，网关模型目录仅证明 `gpt-5.6-sol` 被列出。framework client route 的 plain-text 单次模型探针使用 `max_tokens=32`、`timeout=45`、`max_attempts=1` 和提示 `Reply exactly OK`，结果为 45 秒超时；当时没有 live completion 成功证据，且该失败结果不能由本地 pytest、compile、strict 或 smoke 代替。后续真实端口调用成功的独立证据见 D5 与续跑证据。

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

repo/dev composition 测试校验专用 route 的 model/base/key-env 匹配及 `writer-primary` 隔离。Electron public preview 已证明候选 model/base 通过 preview，但 apply 被当前 release gate 拒绝，持久化 owner 仍为 `revision=0`、`llm=null`，credential 仍未配置。owned-environment admission 污染已修复并有回归测试。模型目录状态为 `listed`，但 plain-text live probe 单次 45 秒超时，不能标记为 live 成功。2026-10-22 续跑通过真实 `OpenAICompatiblePaperAnswerModel`、repo/dev 专用 deployment 和现有网关凭证完成一次文本调用：请求与返回模型均为 `gpt-5.6-sol`，合成上下文为 24 名参与者，回答为 `24 participants.`；耗时 6.75 秒，input/output tokens 为 42/7，单次 transport attempt，使用 deployment 原有 90 秒 timeout 与 256 output-token 上限。该证据证明生产模型端口可实际调用，不代表 Electron 已配置或完成全链路问答。当前 installed release dependency drift 和 credential `configured=false` 仍须分别追踪。只有 release 重建重装、credential provision、成功 apply 和运行时验证全部完成后，才能记录桌面切换完成。

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
- 初次 framework client 的 45 秒短探针超时；续跑已取得真实 paper-answer model port 的成功证据，尚未取得实际 Electron runtime 问答成功证据。
- Electron credential provisioning、成功 apply、实际激活和运行验证尚未完成；在证据产生前，不得宣称 Electron 已切换至 `gpt-5.6-sol`。

## 2026-10-22 续跑证据

- 重新执行已安装宿主 `config-show` 和 `credential-inspect`：仍是 `revision=0`、`llm=null`、`configured=false`，未迁移凭证。
- 通过 `OpenAICompatiblePaperAnswerModel` 的真实远端文本调用成功；合成测试不包含个人或论文私有数据，未输出/持久化 secret。仅将无凭证的结果回执保存在 ignored `.tmp/research-desktop-gpt56-production-port-probe.json`；关键结果已记录在 D5，避免依赖该临时文件作为唯一证据。

## D6：精简文本运行包的可选图像依赖边界

续跑使用受控 Node `24.21.0` / npm `11.19.0`、已有可信 CPython `3.12.14` 和 `windows-x64-text-research-v1` selected Python 输入，在独立目录重建源码 release `20261022T042921Z-38540bff5579`（source revision `6bceda4ca37ad791c9774329945584dd8b21c808`）。portable stage 的真实导入 gate 拒绝该候选：`desktop_app_python_probe_failed`，底层为 `ModuleNotFoundError: No module named 'PIL'`。该候选不安装、不用于激活。

根因是 `backend.research.document.marker_assets` 在 module import 时无条件加载 Pillow，导致未启用 Marker 的文本运行包经 Research composition/API import 也要求图像依赖。Pillow 只用于 Marker 栅格验证，应在实际执行该验证时加载。修复不得向已封存运行包补装依赖、扩大文本 profile 或跳过图像验证；选择图像路径却缺少其依赖时仍须明确失败。原有尺寸、像素、单帧、格式、结尾、解码、bomb-warning 与安全文件边界验证全部保留。

新增隔离子进程回归：拒绝 `PIL` import 时，真实 API composition 仍能 import；实际图像验证不能假成功。代码修复后须重跑匹配图像安全测试、selected Python 真实导入探针与 required smoke，再建立新的源码 release，不能复用修复前的源码身份。

修复后的定向证据：图像安全与独立 no-Pillow 子进程回归 68 项通过；desktop runtime profile/bundle 56 项通过；compile 通过；selected CPython 3.12.14 实际导入 `interfaces.api.app` 成功，174 routes、health route 存在、`PIL` 未加载。完整 required smoke 尚在执行，未将修复前的 release 或任何未完成 stage 标记为可安装。

发布前并行预检：managed Node 24.21.0 / npm 11.19.0 下 desktop typecheck/build 通过，24 test files / 171 tests 通过；固定输入验证通过 33 wheels、33 distributions、3397 RECORD、33 licenses 与 1995 Node files，portable API/PDF/SQLite/TLS/auth/ACL probe 通过。仅为已验证构建输入和打包前检查，不等同于已生成 installer 或已安装。后续构建须绑定包含本修复的 clean 新 HEAD、使用全新独立输出根，并依次执行官方 source release、portable stage、runtime validation 和 dist；不得复用修复前源码身份。

首次完整 smoke（session 45089）已终止：5223 passed、1 failed、1 skipped、23 deselected；失败为 `test_live_production_roots_have_zero_retired_workflow_references`。修正方案保持 scanner 和全部断言不变，移除 Pillow import 的非必需异常包装，在实际栅格验证边界直接导入，让原生 `ModuleNotFoundError` 明确传播；隔离测试须验证原始异常未被包装或吞掉、且不产生 artifact。该次 smoke 不算通过，后续提交前必须重新执行完整 gate。

直接 use-boundary import 修复后的证据：zero-reference scanner 为 0 violations、0 parse failures、fallback=0；全部 architecture 259 项通过，图像/import 与 desktop profile/bundle 共 124 项通过；selected Python 3.12.14 API probe 再次通过（174 routes、health 存在、PIL 未加载）。未修改 scanner、allowlist 或架构断言。第二次 required smoke 使用新日志 `.tmp/research-desktop-optional-dependency-smoke-r2.log` 执行；第一轮失败日志保留，不将其算作通过。

第二次完整 required smoke（session 75484）已成功退出 0：5224 passed、1 skipped、23 deselected，耗时 1772.17 秒；其后 AgentLoop `succeeded`、network_calls=0，sources validation `is_valid=true`、error_count=0、warning_count=0。该 gate 已覆盖修复后的当前源码；后续源码 release 必须绑定本次代码提交的新 HEAD。


## D7：新安装后的 release activation 与 userData 边界（2026-10-22）

从源码 revision `1784cf2cd81b9cc4e9072ff3e3b900b736edcace` 构建的安装器 `Agora-Paper-Agent-0.1.2-x64.exe`（SHA-256 `3a873e83f0135ed541c1791da301ca776626865eb7a434323a55669f17acaf9b`，NotSigned）已按 installer 流程安装至既有 owner root。安装后 installer `admit-owned` 成功；sealed owner 与 14,531-resource runtime payload 通过当前安装清单验证。安装前后（runtime release activation 之前）对 Electron userData 做完整文件清单与 SHA-256 比对：85,061 个文件，新增/缺失/变更均为 0。desktop config 当时仍为 revision 0、`llm=null`，credential-inspect 为 `configured=false`。

安装包内 `PortableBundleManager.verify_bundle()` 只读验证 bundle `desktop-836447922c155244a83b92ec`（14,531 resources；source revision `1784cf2c…`）成功。第一次从仓库工作目录手动调用 packaged `build`，错误为 `desktop_app_release_dependency_drift`，未激活 release。根因是该诊断进程把仓库根目录的 `agora_hub.egg-info` 和 `newsroom.egg-info` 纳入 `importlib.metadata`；这两个仓库 distribution 不属于 bundle 依赖。改为桌面真实启动合同的 resource-root 工作目录进行只读依赖对比后，Python package set、Node/npm 版本与依赖树均匹配 bundle；差异仅为允许 relocation 的 Python/Node/npm executable 路径。

随后通过安装 runtime 提供的 packaged `build` owner（不是启动 Electron GUI）从 resource-root 工作目录执行 bundle 验证、immutable release 绑定和激活成功：release `20261022T054517Z-bc8d21da5385-portable-5492706d55b9`，source revision `1784cf2cd81b9cc4e9072ff3e3b900b736edcace`，dependency digest `c50d361863359c98df345e78628cd85841207ddaecb4443f46952d2aec18c757`。offline doctor 对 frontend standalone closure 与 active production release 均报告通过；整体 doctor 为 warning，仅因模型尚未配置及 Research history owner 尚未初始化。Lifecycle status 为 `STOPPED`，无进程；activation 保留原 release 目录。与安装前 userData 快照再比对：新增 4,708 个文件全部位于 release owner data，`active-release.json` 1 个预期变更，无文件删除，release 目录及 active pointer 以外的 userData 变化为 0。

credential-inspect 仍为 `configured=false`，runtime config 仍为 revision 0、`llm=null`。未执行 credential prompt/provisioning，也未调用 `config-apply`；待明确授权后才能进入凭证和模型配置阶段。故 tasks 4.1–4.2 已完成，4.3–4.5 仍未完成；不宣称桌面生产已切换至 `gpt-5.6-sol`。

## D8：授权后的桌面模型激活（2026-10-22）

用户明确授权将本机 `.env` 中现有 `OPENAI_API_KEY` 写入桌面受控凭证槽。通过已安装 runtime 的 `WindowsCredentialVault` 与桌面 `descriptor_for(data_root)` 写入 `paper-agent/research/openai-compatible`；只输出并验证 `configured=true`，凭证值未写入命令行参数、日志或文档。桌面 `config-preview` / `config-apply` 成功，owner revision 从 0 变为 1，配置为专用 route `research-desktop-answer`、provider `openai-compatible`、model `gpt-5.6-sol`、base URL `https://deedee.tech/v1`，credential binding 继续使用固定槽。配置 owner 创建并激活 config-bound immutable release `20261022T054517Z-bc8d21da5385-portable-5492706d55b9-config-dafc28297a4e`。

随后通过 packaged desktop owner 启动运行时：API、BFF、frontend、raw rewrite 与 runtime health 均为 true；`config-show` 为 revision 1 / `gpt-5.6-sol`，credential-inspect 为 configured。使用实际 `DesktopAppRuntime._api_environment()` 的凭证投影构造 Research runtime settings 后，settings 与部署解析均为 `gpt-5.6-sol`，route 和 base URL 与持久化 owner 一致；只报告 credential presence，不读取或输出值。这验证的是已激活桌面 API 运行配置，不等同于一次已认证的用户 HTTP 问答。

对安装 runtime 的 `OpenAICompatiblePaperAnswerModel` 做了一次无私有数据的合成远端 completion，使用同一桌面 owner 投影的凭证和部署；本次响应为 `transient_network`，未获得模型回答，因此不声称本次桌面凭证下的远端推理已成功。此前 D5 记录的 repo/dev 真实模型端口成功调用仍仅是独立证据。离线 doctor 报告 overall `WARNING`，包含 history/RAG owners 尚未初始化、运行端口已占用等信息；其 Research capability 投影仍显示 `desktop_research_runtime_unavailable`，原因是 doctor 构造 settings 时未使用已授权的 vault presence 投影。运行 API 的配置 owner/runtime settings 已独立解析成功，但 doctor 诊断不应被表述为已修复。桌面服务当前为 `DEGRADED`（非 STOPPED），其 health endpoint 全绿；远端 completion 的网络错误与模型配置/路由解析分开记录。
