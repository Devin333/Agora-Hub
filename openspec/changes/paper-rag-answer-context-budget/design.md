## Context

`AnswerGenerator` 会把 `AnswerContextAssembler` 选出的至多 8 个片段渲染成编号段落，再发给模型。旧的渲染逻辑对每个片段单独截断，截断时并不知道总共有多少段、总长是多少。用评测语料统计：35 个片段中位数 766 字，P90 为 2319 字，有 14 个超过 1000 字，最长一个 13484 字；按题统计，上下文完整长度的中位数是 2091 字。

## Goals / Non-Goals

**Goals**
- 预算放得下时，片段必须原样发送。
- 必须截断时，保留和问题最相关的句子，不受它在片段中位置的影响。
- 每次截断都要记录下来，供阶段 1 统计上下文漏斗。

**Non-Goals**
- 不调整 `AnswerContextAssembler` 选哪些片段、按什么顺序。
- 不处理解析噪声。例如部分片段的 "Equation:" 字段里是一串小写关键词，这个问题另行处理。
- 不引入 tokenizer。预算按字符计，12000 字约合 3000 token。

## Decisions

### D1 总预算与分层 water-filling

`allocate_budgets(lengths, tiers, total, floor)` 的规则：

- 总长不超过预算时，原样返回各片段长度。
- 超出预算时，先给每段预留 `min(length, floor)`，再按 tier 从小到大分配剩余预算：
  - tier 0 是主证据，tier 1 是解释上下文和定位上下文。
  - 每一层内部做 water-filling：短片段先拿满自己的长度，长片段平分剩下的部分。
- 如果连下限都放不下，直接报错。`AnswerGenerator` 在构造时就检查 `max_context_chars >= max_context_chunks × 400`。

默认取值 `DEFAULT_MAX_CONTEXT_CHARS = 12000`、`CONTEXT_PASSAGE_FLOOR_CHARS = 400`。按这批数据，除了那个 13484 字的片段，其他上下文都能整段发送。

### D2 按相关性挑句子

`fit_lines(lines, max_chars, terms)` 的流程：

1. 片段先被渲染成结构化的 `ContextLine(label, text, pinned)`。Title、Image ref、Source locator 这几行是固定行。
2. 全文放得下时，直接原样返回。
3. 放不下时，固定行在预算允许的范围内按顺序保留。
4. 其余行切成句子，按与问题共有的词干数量从高到低排序，分数相同时按原文位置。依次贪心加入，加入后超预算的句子就跳过。
5. 输出时句子按原文顺序排列，句子之间或行首行尾有省略的地方，用 `[…]` 标记。
6. 如果连一个整句都放不下，保留最佳句按单词边界截出的前缀，并在末尾加 `[…]`。

问题词的处理：先去掉停用词和提问用的套话（paper、report、mention 等），再做轻量词干化，去掉 ing、ed、es、s 后缀，要求剩余部分至少 3 个字母。不再注入 result、table 这类泛化词。

断句规则：在 `.!?;` 后断开，后面跟着引号或括号时同样断开。

### D3 记录

`context_metadata.context_budget` 包含 `strategy`、`total_chars`、`original_chars`、`kept_chars`、`truncated_chunk_ids` 和 `passages[]`。`passages[]` 中每项记录 chunk_id、original_chars、budget_chars、kept_chars、truncated。

`context_metadata.prompt_chars` 记录 system 和 user 两部分 prompt 的实际字符数。

## Risks / Trade-offs

- **输入成本上升**：按实际发出的字符数估算，中位数题目的上下文从 1667 字增至 2091 字。网关报告的 token 数无法直接用来衡量这一项，见 R2。
- **R2：网关报告的输入 token 与实际发出的内容不一致**。实测中 prompt 长度只有 1740 到 1766 字的调用，被报成 4730 到 27184 个 token，档位固定，跟 prompt 长度无关。这说明网关侧额外加入了内容。为此新增 `prompt_chars` 字段，成本归因要以它为准，并需要向网关方确认。
- **句子级挑选可能拆散多句才能成立的论证**：原文顺序得以保留，省略处有明确标记；预算放得下时完全不截断。

## Migration Plan

修改后先跑单测和真实语料回归，再用真实模型重跑业务题库，然后跑 smoke，最后提交。回滚只需还原这个提交，不涉及持久化数据。
