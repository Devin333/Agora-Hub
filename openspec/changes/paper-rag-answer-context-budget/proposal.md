## Why

2026-10-22 第一次用真实模型跑业务题库时，有 7 道单篇题的上下文里已经包含 gold 证据，模型却回答"文中没有提到"。逐题审计后确认，其中 5 道（single_019/023/026/027/028）的答案句在生成上下文时就被删掉了，模型根本没看到。原因在 `_compact_context_text`，有两处：

- 每个片段一律截到 1000 字，固定保留开头 55%、结尾 25% 和中间一个窗口。哪怕总预算还很宽裕，也照截不误。这批题里有 17/30 的上下文被删过内容。
- 中间窗口的位置由"问题词首次出现的位置"决定，而问题词里包含 the、for，以及 result、table 这类写死的泛化词。这些词往往在片段开头就能匹配到，于是窗口落在开头附近，跟开头那段重复，夹在中间的答案句反而被删掉。

## What Changes

- **BREAKING**：去掉每个片段固定 1000 字的截断，改为所有被选中的片段共用一个总预算，默认 12000 字。能放下就整段发送；放不下时，先给主证据分配，再给解释上下文，每一层用 water-filling 分配。每个片段至少保留 400 字，被选中的引用不会从 prompt 中消失。
- 单个片段必须截断时，固定行（标题、定位信息）始终保留，然后按"和问题共有的实义词数量"挑选句子，选中的句子保持原文顺序，省略处用 `[…]` 标记。不再使用开头、结尾加窗口的截法，也不再注入泛化词。
- 回答元数据新增 `context_budget`，记录每个片段的原长、预算、保留长度和是否被截断；另新增 `prompt_chars`，记录实际发出的字符数。
- `AnswerGenerator` 的参数 `max_chars_per_chunk` 改为 `max_context_chars`，基准套件的配置项和 CLI 参数同步更名。

## Capabilities

### New Capabilities

- `paper-rag-answer-context-budget`：论文问答上下文的总预算分配、按相关性截断，以及截断记录。

### Modified Capabilities

- 无已归档 spec 需要修改。

## Impact

- 新增 `backend/research/rag/retrieval/answer_context_budget.py`
- `backend/research/rag/retrieval/paper_answer_generator.py`：渲染改为结构化行加共享预算，删除 `_compact_context_text`、`_query_focused_window`、`_context_query_terms`
- `backend/research/rag/evaluation/paper_benchmark_suite.py`、`backend/research/rag/cli/run_benchmark_suite.py`：参数更名
- `backend/research/rag/evaluation/business_answer_eval.py`：效率统计增加 `prompt_chars`
- 用户可见：桌面问答的上下文变长，中位数题目从 1667 字增至 2091 字，回答能引用到片段中部的内容
