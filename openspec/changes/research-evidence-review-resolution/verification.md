# 文档交付与校验记录

日期：2026-09-09。状态：设计文档完成，业务实现尚未开始。

## 本轮交付

- [prd.md](prd.md)：阅读场景、第一性原理、产品流程、角色、状态、26 个验收案例及研究依据。
- [proposal.md](proposal.md)：变更原因、能力与影响范围。
- [design.md](design.md)：版本合同、Graph 接入、确定性判断边界、持久并发、规则发布与迁移。
- 三份 delta specs：`research-claim-review`、`research-review-rule-release`、`research-runtime`，共 18 个 requirement、41 个 scenario。
- [tasks.md](tasks.md)：34 项待实施任务与 AC-01 至 AC-26 覆盖索引，全部未勾选。

## 已完成检查

| 检查 | 结果与适用范围 |
| --- | --- |
| 当前实现只读核对 | 核对 claim 字段、引用验证、报告质量聚合、Graph gate 失败路径、终态发布及人工评审缺口；没有将离线 benchmark 当成在线能力 |
| `openspec validate research-evidence-review-resolution --strict` | 通过；证明 OpenSpec 变更规格通过严格结构校验 |
| `openspec status --change research-evidence-review-resolution --json` | proposal/design/specs/tasks 全部 done；这是提案文档就绪，不代表开发或上线完成 |
| UTF-8、代码围栏与内部链接 | 文档可严格解码，无替换字符；围栏成对，内部链接存在 |
| 规范与任务结构 | 每个 requirement 含 SHALL/MUST 及 scenario，每个 scenario 含 WHEN/THEN；任务 ID 唯一且无实施项勾选 |
| 验收映射 | 26 个 AC 定义齐全，均有对应实施任务 |
| `git diff --cached --check` | 通过；暂存文件仅来自本变更目录，无空白错误 |

## 结论的实际边界

本轮只新增该变更目录中的文档。没有执行数据库迁移、业务代码实现、前端开发、在线模型评测、真实复核或发布；没有改写面试笔记。compile/test/smoke 和浏览器交互验收属于后续实现阶段，本轮不宣称其已通过。

当前仓库同时出现其他前端工作变更；它们不属于本次文档交付，提交必须限定到本变更目录。OpenSpec 目录受仓库 ignore 规则影响，只对本目录作显式纳入，不修改 ignore 配置。

规则、预算和状态为拟实施的产品选择，具体服务配置及定量发布门槛仍须按待办完成。默认关闭、未达门槛不启用 enforce；不能用此文档作为当前系统已解决语义争议的证明。
