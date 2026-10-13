# 集中验证证据索引

截至 2026-10-13，第 1–10 章实现和第 11 章最终集中验证已完成。前端 729 项单元测试、75 项浏览器用例及 typecheck/lint/local-desktop production build 通过；后端最终全量为 9187 passed / 0 failed / 117 skipped / 24 deselected；必需 smoke 为 4192 passed / 23 deselected，AgentLoop offline（network_calls=0）及 sources validation（valid、0 errors、0 warnings）均通过。第 12 章真实使用、恢复与部署验收尚未执行。

- [frontend-release-candidate-validation-20261013.json](frontend-release-candidate-validation-20261013.json)：最终 clean-install、729 项单元测试、typecheck、lint、生产 build、75 项浏览器用例、6 项既有 Studio 跳过、递归依赖树及残余审计告警。
- [frontend-release-candidate-identity-20261013.json](frontend-release-candidate-identity-20261013.json)：最终前端源码和生产构建的逐文件身份；production build 未激活为本机 release。
- [final-automated-validation-20261013.json](final-automated-validation-20261013.json)：本轮集中验证的总记录、实际跳过范围、静态审查和第 12 章待验收边界。
- [spec-validation-final-20261013.json](spec-validation-final-20261013.json)：最终 OpenSpec strict 校验实际返回 valid。
- [final-validation-integration-audit-20261013.json](final-validation-integration-audit-20261013.json)：提交范围、秘密特征、JSON/日志校验和、文档链接及最终 diff 检查。
- [backend-release-candidate-mandatory-smoke-20261013.json](backend-release-candidate-mandatory-smoke-20261013.json)：必需 pre-commit gate 的 compile、pytest、AgentLoop offline 与 sources validation 完整退出证据。
- [backend-release-candidate-full-tests-20261013.json](backend-release-candidate-full-tests-20261013.json)：最终完整后端套件的实际退出结果、117 项跳过原因与冻结源码身份。
- [backend-release-candidate-identity-20261013.json](backend-release-candidate-identity-20261013.json)：最后两处 fixture 修复后的 Python 源码与解释器/依赖身份；最终全量与必需 smoke 使用该冻结候选，检查后逐文件与解释器/依赖重新核对。
- [backend-repaired-candidate-final-validation-20261013.json](backend-repaired-candidate-final-validation-20261013.json)：第二轮失败回归的历史源码身份，不作为最终通过证据。
- [backend-18-failure-repairs-targeted-validation-20261013.json](backend-18-failure-repairs-targeted-validation-20261013.json)：18 项已观察失败对应的 89 项回归通过记录。
- [runtime-recovery-final-contracts-20261013.json](runtime-recovery-final-contracts-20261013.json)：显式关闭 SQLite fixture 连接后，完整 backup/recovery 模块 29 passed；WAL snapshot 与 GC 稳定性断言保留。
- [runtime-all-contracts-after-final-audit-repairs-20261013.json](runtime-all-contracts-after-final-audit-repairs-20261013.json)：Windows runtime/恢复合同 138 passed；2 项 symlink 权限环境跳过保留原始理由。
- [backend-full-validation-failed-attempt-20261013.json](backend-full-validation-failed-attempt-20261013.json)：首个完整后端回归的 18 failed / 9167 passed / 117 skipped / 24 deselected，仅为失败历史，不能作为最终通过证据。
- [backend-full-validation-second-failed-attempt-20261013.json](backend-full-validation-second-failed-attempt-20261013.json)：第二轮完整回归的两项失败、117 项实际跳过理由及冻结源码绑定；仍为失败记录。
- [validation-queue-20261012.json](validation-queue-20261012.json) 与 [validation-root-cause-repairs-20261013.json](validation-root-cause-repairs-20261013.json)：执行历史、实际根因修复和当前验证状态。

早期 `candidate-freeze-before-validation`、`frontend-final-validation`、首次浏览器诊断和 Next 14 检查属于历史记录，后续修复已产生新的源码与检查身份。应按上述最终候选记录及其状态判断结果，不将早期同名 “final” 文件视为最新结果。

日志以 `.log.gz` 保存精确原始字节，gzip 的时间字段固定为 0。归档字节 SHA-256 和解压后的原始日志 SHA-256 见 [validation-log-archives-20261013.json](validation-log-archives-20261013.json) 及各记录的 `logArchiveIdentity` / `decompressedSha256`；运行时元数据中的原始 TEMP 路径和原始日志校验和保留。没有修改日志文本、测试选择或断言来通过检查。

浏览器 route fixture、fake worker/repository 与自动合同测试仅证明对应的自动验证行为。真实账号、provider、PDF、资料备份与隔离恢复、实际 release 启动、预算和用户电脑重启证据按第 12 章分别核销。
