## 1. 根因审计

- [x] 1.1 逐题核对 7 道"上下文含 gold 仍拒答"的题：5 道答案句被截掉，2 道未截断（single_004/009）
- [x] 1.2 统计语料片段长度与按题上下文长度，确定默认预算

## 2. 实现

- [x] 2.1 新增 `answer_context_budget.py`：`allocate_budgets`、`fit_lines`、`question_terms`、`ContextLine`
- [x] 2.2 `AnswerGenerator` 改为结构化行加共享预算，参数改为 `max_context_chars`；删除旧的固定窗口渲染
- [x] 2.3 记录 `context_budget` 与 `prompt_chars`；业务题库效率统计增加 `prompt_chars`
- [x] 2.4 基准套件配置项与 CLI 参数同步更名

## 3. 验证

- [x] 3.1 单测：预算分配、按相关性截断、顺序保持、单句超长兜底，以及 5 道审计题的真实语料回归
- [x] 3.2 用 `gpt-5.6-sol` 重跑业务题库并逐题对比，见 `evidence/business-qb-context-budget-20261022.md`
- [x] 3.3 核对异常输入 token：预算从未超出，异常来自网关侧
- [x] 3.4 `openspec validate paper-rag-answer-context-budget --strict`
- [x] 3.5 `python -m scripts.dev smoke`
- [x] 3.6 提交
