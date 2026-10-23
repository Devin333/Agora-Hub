## 1. 规划器与 session 终态

- [x] 1.1 `DeterministicRAGPlanner` 按 D6 依据 `executed_queries` 选择首个未执行的查询变体
- [x] 1.2 `_handle_plan_failure`：只有 dedup 失败且无重新规划预算时，决策为 `INSUFFICIENT_EVIDENCE`；其他 plan gate 失败仍为 `HALTED`
- [x] 1.3 新增 `AnswerWorkerError`；`_run_generation_phase` 捕获后记为 `FAILED`，并写入 `rag_answer_generation_failed` 事件
- [x] 1.4 answer gate 失败的终态由 `ABSTAINED` 改为 `HALTED`
- [x] 1.5 `GroundedAnswerCandidate.unresolved_citations`，以及对应的 `RAGAnswerGate` 引用完整性检查
- [x] 1.6 规划器、session、gate、replay 的单测

## 2. 回答生成链路

- [x] 2.1 新增 `PaperAnswerModelPort`、请求/响应/错误类型
- [x] 2.2 新增 `OpenAICompatiblePaperAnswerModel` 适配器和 `build_unity_paper_answer_model`
- [x] 2.3 `AnswerGenerator`：Research 系统指令（含不可信数据声明）、拒答前缀协议解析、合法与越界引用编号、usage；删除 `_repair_generated_answer` 及其专用辅助函数
- [x] 2.4 `PaperAnswerWorker`：删除关键词拒答和 negative-presence 判定；系统问题抛 `AnswerWorkerError`；越界引用写入 `unresolved_citations`
- [x] 2.5 composition、`paper_rag_factory`、数据脚本改用回答模型端口
- [x] 2.6 生成器、worker、个人记忆、composition 的单测

## 3. 评测侧

- [x] 3.1 `AnswerOutcome`；`AnswerMetricCase.outcome` 改为必填；删除关键词拒答判定
- [x] 3.2 `score_answer_case` 按 D7 打分；新增失败原因 `system_failure`、`abstained_over_conservative`、`abstention_wrong`
- [x] 3.3 `EvidenceAnswerSample.outcome`；平均值返回 `None`；新增 `outcome_counts`、`system_failure_rate`、`over_abstention_rate`；报告显示分母
- [x] 3.4 scorecard 适配器和报告 dict 处理 `None`
- [x] 3.5 live 评测：统一 `run_id` 并绑定语料；注入回答模型；按 session 状态映射 outcome，不合成文本
- [x] 3.6 基准套件 `generation_track`，gold 注入只在 `oracle_context` 轨道；引用改用 `cited_context_indexes`
- [x] 3.7 评测侧单测，以及离线复现验证（golden corpus 加假模型，确认不再全部中止）
- [x] 3.8 修正集成测试：用 `metrics.gate_failures_by_gate` 与 `accepted_evidence_count` 断言无 dedup 失败、无中止且检索非空；分别回退规划器修复与 run_id 绑定验证测试会失败
- [x] 3.9 评测生成模型与桌面 Research 路由共用 `llm_selectors` 选择键（`NEWS_RESEARCH_LLM_*` 优先），judge 仍走 `OPENAI_*`；测试隔离仓库 `.env`

## 4. 验证与交付

- [x] 4.1 `openspec validate paper-rag-answer-outcome-integrity --strict`
- [x] 4.2 `python -m scripts.dev smoke`
- [x] 4.3 提交

## 5. 首次真实数据

- [x] 5.1 评测 judge 独立选择键 `NEWS_RAG_EVAL_JUDGE_*`（必须显式配置，不回退 `OPENAI_MODEL`）；基准套件与数据脚本的 judge 改用 `build_unity_judge_llm_call`
- [x] 5.2 业务题库加载器：只评分已审阅单篇题，草稿与多论文题显式记为跳过并报告覆盖率
- [x] 5.3 业务题库运行器与 CLI `run_business_answer_eval`：走生产门控回答链路，记录每题 outcome、端到端与生成延迟、token
- [x] 5.4 用 `gpt-5.6-sol` 跑通 42 题，结果摘要见 `evidence/business-qb-live-20261022.md`
- [x] 5.5 是否题的"明确否定 + 引用"判为拒答正确，记录 `abstention_basis`（refusal / cited_negation）；重新打分 2026-10-22 运行，拒答准确率 9/11 → 11/11
- [x] 5.6 记录网关对生命科学类问题的内容拒答限制（无法规避，表现为 halted）



## 6. 业务题库父子重复片段判分修正（2026-10-23）

- [x] 6.1 从 corpus snapshot 构建同论文、正文空白归一后相同的父子片段等价闭包；接入业务题库运行器，保留 strict gold 和 strict 指标，补充真实语料与边界回归测试
- [x] 6.2 删除原文未说明任务名称的 `single_007`，将题库覆盖更新为 89 行、41 道可评分题（单篇 29、拒答 12），保留其他题目的标注
- [x] 6.3 使用已保存的真实模型回答或离线样本验证等价评分差异，明确离线重评分不等于新一轮真实模型评测；更新证据与交接文档
- [x] 6.4 运行定向测试、`openspec validate paper-rag-answer-outcome-integrity --strict`、`python -m scripts.dev smoke`，复核后提交
