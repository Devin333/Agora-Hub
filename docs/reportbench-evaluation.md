# ReportBench cross-source evaluation adapter

本项目当前把 ReportBench 接入限定为**跨来源研究报告产物的离线适配与合同校验**，不把它误当作项目质量 gate，也不会在普通命令中调用模型、网页抓取器、judge 或付费 API。

## 输入文件

### 题目 JSONL

每行一个题目：

```json
{"task_id":"case-1","question":"What does the evidence show?"}
```

### 结果 JSONL

每行一个完成的跨来源研究结果。`sources` 必须是结构化来源，不能只依赖 Markdown 正文中的链接：

```json
{"task_id":"case-1","question":"What does the evidence show?","report":"# Findings\n...","sources":[{"source_id":"paper-1","url":"https://example.org/paper","title":"Paper","kind":"paper"}],"citations":[{"source_id":"paper-1","locator":"p. 3"}],"execution":{"model":"offline-fixture"},"status":"succeeded"}
```

支持的来源类型包括 `paper`、`article`、`website`、`dataset`、`repository`、`report` 和 `other`。来源 URL 必须是 HTTPS；同一结果中不能有重复来源 ID 或规范化 URL。

## 命令

所有命令均为离线命令：

```powershell
python -m scripts.dev reportbench validate `
  --tasks path/to/tasks.jsonl `
  --results path/to/results.jsonl

python -m scripts.dev reportbench export `
  --tasks path/to/tasks.jsonl `
  --results path/to/results.jsonl `
  --output .newsroom/eval/reportbench/results.jsonl

python -m scripts.dev reportbench run-plan `
  --tasks path/to/tasks.jsonl `
  --task-id case-1 `
  --task-id case-2 `
  --output .newsroom/eval/reportbench/run-plan.json `
  --budget-usd 0
```

`validate` 和 `export` 要求每道题都有且仅有一个结果；`export` 会拒绝覆盖内容不同的已有文件。`run-plan` 只生成有界 manifest，包含 `requires_approval=true`、`execution_started=false`、`qualification_status=not_qualified`，不会启动真实研究流程。

## 与真实跨来源流程的连接

后续接入真实 Guided Research runner 时，应通过 `CrossSourceResearchRunner` 注入，返回 `CrossSourceResearchResult`，再由 Harness 在显式批准的 run plan 下执行。当前变更不修改生产报告生成、质量 gate、存储或 publication 路径，也不复制外部 benchmark 的题目和参考答案到生产代码库。

离线导出的结果只能证明产物满足适配器合同，不能据此宣称 ReportBench 分数或研究质量合格。正式 benchmark judge 和任何产生费用的运行应另开、显式批准的任务执行。
