## Context

项目已有 Research RAG 评测协议和 `backend/research/benchmark/` 领域模型，但这些能力主要服务单篇论文 RAG 或内部数据集校验。跨来源报告流程的真实产物由 Guided Research/报告层产生，包含报告正文、来源 URL、来源类型及运行诊断；外部 ReportBench 需要按题目逐行接收可复现的报告记录。当前没有一个位于领域层与 CLI 之间的稳定转换边界。

本变更必须在当前工作区大量未提交改动的情况下安全实施，不能覆盖用户改动，也不能把外部 benchmark 参考答案、题目金标准或 judge 结果写入生产索引。普通编译、测试和 smoke 必须继续无网络运行。

## Goals / Non-Goals

**Goals:**

- 定义独立于具体 UI、executor 和存储的 `CrossSourceResearchRunner`/结果合同。
- 将项目结果规范化为 ReportBench-compatible JSONL，同时保留项目运行身份、来源和成本/延迟元数据。
- 对题目输入、输出记录、URL、引用、重复 ID 和不可公开数据执行确定性离线校验。
- 提供 CLI 的 validate、export 和 run-plan/manifest 入口；真实 run 默认需要显式 dry-run/approval 边界。
- 通过 fixture 和单元测试覆盖成功、缺字段、重复、非法 URL、失败运行和多来源报告。

**Non-Goals:**

- 不在本变更中实现 ReportBench 的外部 judge、网页抓取或付费模型调用。
- 不改动生产 Research、Guided Research、RAG、质量 gate 或报告生成算法。
- 不复制 ReportBench 全量数据、参考报告或评分标准到生产代码库。
- 不以离线格式校验结果宣称模型或产品质量合格。

## Decisions

1. **采用 domain-neutral result contract，而不是直接导入 ReportBench SDK。**
   - 适配器以题目 ID、问题文本、Markdown 报告、结构化来源和运行元数据为核心；ReportBench 的官方 JSONL 只是一个导出格式。
   - 这样可以在外部 benchmark 版本变化时保留项目内部运行记录，并支持后续 DRACO/DeepResearch Bench II 导出。
   - 不采用直接复制 benchmark 仓库到项目依赖的方案，避免数据泄漏、版本耦合和新增必需网络依赖。

2. **通过注入 runner 连接真实研究流程。**
   - CLI 的离线校验/导出路径只处理已落盘的结果；runner 由 composition 层提供，返回一个规范化 `CrossSourceResearchResult`。
   - benchmark 层不直接实例化 `GuidedResearchService`、LLM client、网页抓取器或数据库。
   - 这样保持 interface 调 application/service、Harness 控制流程和评测不写生产状态的边界。

3. **报告正文与来源分离，并强制结构化引用可追踪。**
   - 每个来源记录包含稳定 source ID、URL、标题、来源类型和可选 locator；报告可以是 Markdown，但来源不能只从 Markdown 正则猜测。
   - 缺少结构化来源时记录为 contract failure，而不是默认为零分或自动补全。

4. **命令分为 validate/export 和 run-plan。**
   - `validate` 只做本地确定性检查，报告 `network_calls=0`、`paid_calls=0`。
   - `export` 只写指定输出文件并拒绝覆盖不同内容的既有文件。
   - `run-plan` 只生成待运行 manifest，必须显式传入 runner/approval 才能执行，第一版不提供隐式付费执行。

5. **数据保留采用逐行 JSONL 与 manifest。**
   - 每个任务恰好一条输出，按 task ID 唯一；manifest 固定 benchmark、schema、代码版本、生成模型、时间、预算和执行身份。
   - 原始报告不进入 Git；测试只使用最小合成 fixture，避免泄露外部 benchmark 内容。

## Risks / Trade-offs

- [外部 ReportBench schema 变化] → 内部合同固定版本并将外部导出映射集中在一个 adapter；validate 输出明确 schema/version。
- [项目报告没有结构化 citation] → validate 失败并要求 runner 提供来源，不从正文猜引用，避免虚假评测。
- [运行结果被误认为正式质量成绩] → 所有离线结果标记为 `diagnostic`/`not_qualified`，CLI 输出明确不做 quality gate 决策。
- [用户当前工作区存在大量改动] → 只新增本变更路径和明确相关文件，提交前按路径暂存，禁止 reset/clean。
- [误触发付费 API] → 默认命令不创建模型 client、不联网；真实执行必须有显式 run manifest、预算和后续审批。

## Migration Plan

1. 创建 adapter/domain contract、CLI 和离线 fixture。
2. 运行 OpenSpec 严格校验、编译、相关单元测试和 smoke。
3. 先用项目已有的 3 道内部题生成结果文件，再用 `validate`/`export` 检查格式；此步骤不调用外部 judge。
4. 后续另开显式运行任务，把真实 Guided Research runner 接入 manifest，并在批准预算后运行 3 题 canary。
5. 若适配器不满足要求，删除本分支新增路径即可；不影响现有 Research/RAG 运行。

## Open Questions

- Guided Research 最终报告产物的稳定 run/result 读取接口需要在接真实 runner 时确认，不能由本适配器猜测。
- ReportBench 当前版本的完整官方字段与评测命令在实际下载版本中最终核对；内部 schema 不因外部字段缺失而放宽来源合同。
