# 业务题库父子重复片段修复交接（2026-10-23）

## 当前范围

已落实用户确认的两项修复：父子重复片段算等价引用，删除缺陷题 `single_007`。接续的 OpenSpec change 为 `paper-rag-answer-outcome-integrity`，本次任务见 section 6。旧的 `codex-handoff-paper-rag-answer-outcome-integrity.md` 描述的是更早阶段，不应将其中“未提交”“无法 import”等历史状态当作当前状态。

## 实现

- `backend/research/rag/evaluation/business_question_bank.py`：`load_business_question_bank(..., chunks=...)` 从 snapshot 结构建立等价闭包。必须同论文、有父子边、非空正文经空白归一后相同；双向、多层、抗环。不同正文、无亲缘或跨论文片段不扩展。
- 原始 `gold_chunk_ids` 和 strict 指标保持不变，替代 ID 只进入 `equivalent_gold_chunk_ids`。未知 gold 或没有 corpus 时仍精确匹配。
- `backend/research/rag/evaluation/business_answer_eval.py` 先加载 snapshot，再将真实 chunks 交给 loader，确保运行器使用等价评分。没有向生产检索或模型上下文注入 gold。
- `data/eval/business_question_bank_v1_draft.jsonl` 仅删除 `single_007`，其他行保持不变。原文只说“the task”而没有任务名，不能作为应回答题。当前 89 行，41 可评分（单篇 29、拒答 12），48 草稿跳过。

## 离线重评分

没有重新调用模型。对已保存运行 `business_question_bank_v1_live_20261022T202201Z` 的 42 条真实回答去掉 `single_007`，用相同的当前 scorer 比较启用等价前后：成功数 **36/41 → 39/41**，引用 gold 覆盖 **89.3% → 100%（n=28）**，strict 覆盖保持 **89.3%**。只改变 `single_001/002/008`。

证据：`openspec/changes/paper-rag-answer-outcome-integrity/evidence/business-qb-equivalence-replay-20261023.md` 及同名 JSON。JSON 保留完整可重评分样本和输入哈希；本地原始运行工件仍在 `.newsroom/eval/business_question_bank_v1_live_20261022T202201Z/`，没有覆盖。

## 验证与提交

- 项目虚拟环境 `.venv/Scripts/python.exe`，四个定向测试文件 **60 passed**：`test_business_answer_eval.py`、`test_answer_context_budget.py`、`test_paper_evidence_type_coverage.py`、`test_run_evidence_eval.py`。
- compile、OpenSpec strict 校验、`git diff --check` 已通过；从入库证据 JSON 独立复算全部 41 题的两组共 82 个评分，结果一致。
- 完整 `python -m scripts.dev smoke` 已通过：5284 passed、1 skipped、23 deselected、61 warnings，退出码 0；运行链路 smoke 为 succeeded（network_calls=0），source 校验 error/warning 均为 0。完整日志路径与哈希见证据 JSON 的 `verification`。

## 下一步：阶段 1

本次不启动 QASPER，也不声称已通过正式质量评测。后续按原计划在 QASPER dev 分层抽取约 300 题，走生产 `PaperRAGSession` 端到端路径，产出质量、拒答、系统失败、引用、延迟和 usage 指标，并保存逐题 trace。生物医学类网关内容拦截须单独分层，`halted` 计为系统失败，不混入拒答正确率分母；不得尝试绕过网关。

当前离线样本尚有 `single_009` 过度拒答和 `abstain_002` 系统中止。这些应在后续真实评测中观察，不应通过放宽门禁或改写模型回答来“修分”。fixture 检索结果不代表生产 embedding，业务题也没有事实级标注，不能将 95.1% 当作事实准确率或发布依据。
