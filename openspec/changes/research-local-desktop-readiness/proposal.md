> **已被取代（2026-10-28）**：`hosted-paper-agent-service` 用服务器上的 `hosted` profile 取代 `local-desktop` profile。本变更的本机运行工具、loopback 认证与 cookie 策略、本机备份恢复和版本发布合同已不再适用，相关实现（`scripts/local_desktop`、`framework/shared/local_desktop.py`、`local-desktop.ps1`）已删除。Research 首页、阅读、导入、报告等功能要求由 hosted 变更承接。未完成任务不因取代而视为完成。

## Why

Research 已有真实来源、解析、Harness 分析、质量门、持久化与工作台组件，但尚未完成个人 Windows 电脑上的端到端交付：正式入口、账号身份、研究报告接线、进程管理和恢复演练仍存在缺口。用户需要可以每天打开、处理真实论文、保存研究结果并在重启后继续工作的本机应用。

## What Changes

- 为显式 `local-desktop` profile 建立正式 Research 首页、论文阅读、材料分组和报告入口；迁移既有研究深链接与历史记录，保留其查询和续读语义。
- 启用本地用户名密码、首次账号初始化与服务端 owner 校验；使同源代理、后端身份和仅限 loopback 的 HTTP cookie 策略一致，公共部署的认证要求保持有效。
- 补齐真实来源搜索、链接/PDF 导入、解析反馈、笔记、精确续读、材料复用、保存冲突及错误恢复，承接已有工作台实现和未完成验收。
- 将现有 Research 单论文分析、问答、质量结果、run/artifact 查询接入前端；实现可追溯报告查看、保存引用及 Markdown/JSON 导出。多材料报告保留显式草稿与确定性材料整理，不承诺本次新增跨论文自主综述引擎。
- 提供 Windows 本机运行工具：配置诊断、生产构建、启动、停止、重启、状态、日志和浏览器入口；固定项目 `.venv` 与依赖版本，后台进程只监听本机，解析组件按真实能力使用 Docker。
- 沿用现有 auth/workspace/Research/Harness 存储，实现一致性备份、校验恢复及兼容的已验证版本恢复；不建立第二套业务 run/event 数据库。
- 建立同一候选版本的自动检查、真实浏览器、真实来源/provider、故障、重启和恢复证据。遵循用户顺序：全部生产代码及测试代码完成后，再集中运行测试，之后验收与部署。
- 本轮仅提出规格与实施任务，不启动服务、初始化用户、修改真实配置或执行上线。

## Capabilities

### New Capabilities

- `research-desktop-workspace`: 本机正式入口、已认证工作区、真实来源、材料持久化、可恢复桌面交互与旧链接迁移。
- `research-desktop-run-report-flow`: 从明确的单论文任务进入既有 Research/Harness 路径，展示真实状态、可核验报告和受控恢复。
- `research-local-runtime-management`: loopback profile、配置诊断、Windows 进程生命周期、生产构建和本机操作入口。
- `research-local-data-recovery`: 既有存储的一致性备份、可验证恢复和兼容版本恢复。
- `research-desktop-release-acceptance`: 实现后集中测试、真实桌面验收与本机交付证据的完整门槛。

### Modified Capabilities

None. 本变更新增桌面交付合同，消费现有 Research runtime、run persistence、Harness gates 和工作区合同，不修改其权威边界。`research-runtime` 中既有后端变更的 UI 排除范围不阻止本独立前端变更。

## Impact

- **Frontend:** `frontend/src/app`, Research/reader/report/auth components, BFF routes, navigation and workspace persistence bindings; `frontend/next.config.mjs` and root environment loading.
- **Application/API:** `interfaces/services`, `interfaces/api`, `interfaces/composition` 中的已认证接线、能力投影、运行查询和报告投影；必要时扩展现有 `backend/research` application ports，禁止接口直达 executor/store。
- **Local operations:** 新的 Windows lifecycle entrypoint 与 Python 实现、脱敏配置样例、运行记录、日志轮换、备份清单及本机使用说明。复用现有 CLI `api serve` 和 frontend package scripts。
- **Persistence:** 现有 `.newsroom/auth`、workspace/history、Research imports/results 与 Harness artifacts/events 等实际解析的存储目录；路径所有者与一致性边界必须清点，不假定备份一个目录即可恢复全部数据。
- **Dependencies:** 本机现有虚拟环境和前端 lockfile；真实网络/provider 配置；仅对所选解析路径要求 Docker/模型。不默认引入 Redis、PostgreSQL、Qdrant、Windows service wrapper 或反向代理。
- **Existing changes:** `research-workspace-efficiency` 5.2/5.3 保留原所有权，可引用本变更相同候选版本的证据。并行编排、durable events 的部署资格和质量基础的条件任务继续由原 change 管理，不以本机验收替代。
- **Scope limits:** Windows 单机个人使用与桌面浏览器；不含公网/局域网开放、多机部署、移动端重做、全站重构、外部发布或未获准的并行/检索/上下文实验启用。
