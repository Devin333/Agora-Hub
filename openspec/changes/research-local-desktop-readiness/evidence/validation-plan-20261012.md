# 集中验证与真实验收计划

日期：2026-10-12。此文件是待执行计划，不是通过记录。准入条件是 tasks.md 的 1–10 章生产实现与测试代码全部经静态审查完成；第 11 章开始前不运行应用、测试、typecheck、lint、build、compile、smoke 或 live probe。

## 候选冻结与证据

冻结实际变更文件清单、内容摘要、frontend package-lock、项目 .venv 解释器/Python resolved dependencies、Node/npm、单一解析后的非秘密 profile/config identity。Git revision 加完整源码摘要共同识别未提交候选，不能只用父提交作为新实现身份。任何根因修复产生新候选，都需重新绑定最终证据。

命令记录保留 cwd、脱敏环境键名、开始/结束时间、退出码、测试计数和受控日志引用。失败、跳过、未执行分别记录；之前 change 的成功记录不能证明本候选通过。报告不包含秘密值、会话、原始论文全文或模型 prompt。

## 第 11 章执行顺序

1. 静态验收实现清单、生产 source 路径、依赖方向、所有权、DTO 和坏路径处理。确认三个实施组没有未完成代码，写明每个 requirement 对应实现和测试文件。
2. frontend 使用实际 lock 安装的依赖，执行 npm.cmd run test、npm.cmd run typecheck、npm.cmd run lint、npm.cmd run build。build 显式采用 local-desktop/canonical origin 生产配置；后续 release manifest 必须记录该配置和新构建身份。禁止旧 .next 作为通过证据。
3. 后端使用 F:/github/NewsRoom/.venv/Scripts/python.exe，执行 -m scripts.dev compile、-m scripts.dev test、-m scripts.dev smoke。现有全量默认排除 opt-in live_research_e2e；这是离线回归边界，不能据此宣称真实 provider 成功。
4. 全量已纳入的新 auth/request-admission、Windows lifecycle/backup/recovery 合同测试若已执行成功不重复运行；browser 回归按实际 Playwright 配置明确服务器/端口/数据隔离，自动 fixture 与真实 API 证据分开标识。
5. 执行 openspec validate research-local-desktop-readiness --strict，并验证任何实际修改的其他 active change。审计 tasks 的实现完成与最终验收状态。
6. 根因修复后重跑受影响检查；若共享 API/runtime/BFF 合同或最终 release 代码改变，最终 frontend 全量、backend 全量/smoke 和生产构建需覆盖新候选。全部必需检查通过再提交代码。

## 第 12 章真实流程与预算

生产 release 使用明确的构建/依赖身份和 loopback 起停工具。真实成功流程不得 route mock、注入fixture、伪造正文/质量或用测试 worker；故障测试与真实成功证据区分。

首轮计划只选一篇可公开获取的真实论文和一份真实文本 PDF；arXiv/DOI/GitHub 引用分别走真实已有入口。分析最多提交一次，问答最多提交一次；响应未知时按已持久化 requestId/generation 查询，不重复付费执行。重试必须先审查原结果且由用户创建明确新身份。probe 的请求/超时/token/费用上限由实际配置与服务预算约束，未配置价格不能承诺成本界限。

实际验收前应把来源、合法已准备 parser/provider、预算、目标 release、隔离业务根和有限清理范围写入执行记录。不能在正常 start 自动下载模型或启用实验。真实账号初始化仅在从未初始化时发生，不覆盖既有账号；公开缓存和外置凭据作为明确依赖，不伪称备份自包含。

数据恢复同时核对材料、笔记、论文正文、run/report、artifact 引用、会话撤销及旧请求 fencing。恢复校验进程仅绑定恢复 roots；原业务根不可读时仍需真实读取资料。兼容 rollback 使用两个验证过的 release；不兼容降级保留拒绝证据并返回候选版本。

真实电脑重启必须由用户实际执行，不自动重启。未发生时 AC-15 和 task 12.12 保持未完成；service restart 不替代。自启默认关闭，只有用户明确启用后管理当前用户本实例启动项。

## AC 证据与失败边界

AC-01/17 绑定配置/诊断、授权、网络监听与脱敏；AC-02/03/04 绑定账号、路由迁移和 workspace CAS/owner；AC-05/06/07 绑定真实来源、import/source revision/正文和续读；AC-08/09/10/11 绑定 request/run、quality、artifact 与报告导出；AC-12/13/14 绑定实际 Windows 进程、备份/恢复和两个 release；AC-15 单独人工实机记录；AC-16 绑定 1366×768、1920×1080、125% 缩放和键盘/错误状态。

环境缺失、预算不足或必须由用户完成的实机动作须保留未完成项及具体阻碍。每项 AC 使用同一最终候选证据，原 workspace 5.2/5.3 只核销实际满足部分，其他 change 的发布资格和实验开关不继承。
