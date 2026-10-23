## ADDED Requirements

### Requirement: Query vectors may be embedded live and are persisted

查询向量 MUST 按"基础缓存 → 补充缓存 → 实时调用 embedding 端口"的顺序解析。实时返回的向量 MUST 先归一化，再写入补充缓存；同一查询再次出现时 MUST 直接复用缓存中的向量，MUST NOT 重复调用。chunk 向量和字段向量 MUST 只从离线缓存读取。

#### Scenario: Cache miss is embedded once
- **WHEN** 同一个未缓存的查询被检索两次
- **THEN** embedding 端口 MUST 只被调用一次
- **AND** 解析器统计中实时调用 MUST 为 1，补充缓存命中 MUST 为 1

#### Scenario: Identity mismatch
- **WHEN** 补充缓存记录的基础缓存 SHA-256、模型 ID 或维度与当前不一致
- **THEN** 解析器 MUST 抛出错误，MUST NOT 使用补充缓存中的向量

#### Scenario: Offline default unchanged
- **WHEN** 构造 fixture store 时没有传入查询向量解析器
- **THEN** 缓存未命中时 MUST 像原来一样抛出 `QasperOfflineCacheError`

### Requirement: Stratified deterministic sampling

抽样 MUST 按第一位标注者的答案类型（extractive、abstractive、boolean、none）分层，每层按 `sha256(seed:case_id)` 排序后取前 N 道。抽样结果 MUST 只由题目和种子决定。

#### Scenario: Same seed, same sample
- **WHEN** 用相同的数据集和种子抽样两次
- **THEN** 两次的抽样清单 MUST 完全一致

#### Scenario: Short stratum
- **WHEN** 某一层的题数少于 N
- **THEN** 该层的题 MUST 全部入选，清单 MUST 记录该层的实际数量

### Requirement: Official Answer-F1 with explicit projection

Answer-F1 MUST 与 QASPER 官方评测脚本保持一致：规范化方式、token F1、在多个标注者答案中取最高分。预测的投影规则如下：

- 拒答或证据不足 MUST 投影为 "Unanswerable"；
- 系统失败 MUST 计 F1 为 0，并单独统计；
- 问题以助动词开头、且回答以明确的 Yes/No 开头时，MUST 投影为 "Yes" 或 "No"；
- 引用标记 MUST 在打分前去除。

#### Scenario: Abstention on unanswerable gold
- **WHEN** 参考答案是 "Unanswerable"，系统结果是拒答
- **THEN** 该题的 Answer-F1 MUST 为 1.0

#### Scenario: Yes/no projection
- **WHEN** 问题为 "Do they use attention?"，回答为 "Yes, the model uses attention [1]."，参考答案为 "Yes"
- **THEN** 该题的 Answer-F1 MUST 为 1.0

#### Scenario: Matches official scorer
- **WHEN** 对相同的预测和参考答案分别用本实现和官方 `qasper_evaluator.py` 计算
- **THEN** 两者的 Answer-F1 MUST 一致

### Requirement: Coverage only on valid mappings

引用覆盖和上下文覆盖 MUST 只在冻结映射为 valid 的题上计算，并 MUST 分别报告 strict 和 equivalent 两种口径。映射为其他状态的题 MUST 记录原状态，MUST NOT 计入这两项的分母。

#### Scenario: Needs-review case
- **WHEN** 一道题的映射为 needs_review
- **THEN** 该题的引用覆盖 MUST 为 None，且 MUST NOT 计入引用覆盖的分母

### Requirement: Judge verdicts are strict

judge MUST 只对实际作答的题调用，并且 MUST 输出包含 `correct` 和逐条 `citations[].supports` 的严格 JSON。解析失败或调用失败时 MUST 记为 `judge_error`，MUST NOT 计入任何正确率。

#### Scenario: Malformed judge output
- **WHEN** judge 返回的内容不是合法 JSON
- **THEN** 该题 MUST 记为 judge_error，且 MUST NOT 计入 judge 正确率的分母
