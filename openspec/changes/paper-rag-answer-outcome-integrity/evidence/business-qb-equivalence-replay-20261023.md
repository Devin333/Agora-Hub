# 业务题库父子重复片段离线重评分（2026-10-23）

## 方法与范围

- 输入：本地已保存的 `.newsroom/eval/business_question_bank_v1_live_20261022T202201Z/answers.jsonl`（42 条）。原运行使用真实生成模型 `gpt-5.6-sol`；本次新增模型调用数为 **0**，没有覆盖旧工件。
- 删除用户已确认有缺陷的 `single_007` 后，两组均对相同的 41 条历史回答使用当前 scorer。已提交的“明确否定 + 引用”规则在两组中均生效，因此本比较只测量父子等价规则，不与旧报告的 42 题数字混算。
- 对照组：题库加载器不传 chunks。修正组：加载同一 golden corpus snapshot，把 chunks 传入题库加载器，再构造 `EvidenceAnswerSample`，调用 `EvidenceAnswerEvaluator.evaluate`。
- 两组逐题核对问题、paper、expected_behavior、原始 gold ID 与保存的运行一致；严格引用覆盖逐题保持不变。所有等价集合均预先由 corpus 结构产生，不参考回答或命中结果。
- 完整可审计样本、输入 SHA-256、覆盖率及逐题前后差异见同目录 `business-qb-equivalence-replay-20261023.json`。JSON 中保留原始回答、outcome、引用及上下文 ID，可在没有本地 `.newsroom` 工件时重建评分样本。

## 结果

| 指标 | 不启用等价 | 启用等价 | 分母 |
|---|---:|---:|---:|
| 确定性回答成功数 | 36（87.8%） | 39（95.1%） | 41 |
| 引用 gold 覆盖 | 89.3% | 100.0% | 28 道期望回答且确实作答的题 |
| strict 引用 gold 覆盖 | 89.3% | 89.3% | 28 |
| 拒答准确率 | 100.0% | 100.0% | 11；另 1 道系统失败不计入 |
| 系统失败率 | 2.4% | 2.4% | 41 |
| 过度拒答率 | 3.4% | 3.4% | 29 |

仅 `single_001`、`single_002`、`single_008` 的引用覆盖从 0 变为 1，`citation_missing_source` 被清除，`answer_success` 变为 true。三题的 strict 引用覆盖仍为 0。剩余失败：`single_009` 为 `abstained_over_conservative`；`abstain_002` 为 `halted/system_failure`，不得计成正常拒答。

题库现为 89 行：41 道可评分（单篇 29、拒答 12），48 道草稿显式跳过。语料中只有三组同正文父子重复片段被纳入：

- `chunk_eb320183e948` / `chunk_80edd06882c4`
- `chunk_34a0b45c1acd` / `chunk_2b509ff3c83d`
- `chunk_f6a8eee4a2c6` / `chunk_d1d5933ef2f0`

## 结论边界

本次只修正评测口径和题库缺陷，未修改生产检索、上下文、生成及门禁。原始检索使用 fixture 哈希索引，不代表生产 embedding 质量；业务题没有 `answer_facts` 标注，成功率也不能替代事实正确性评估。QASPER 阶段 1 和新的真实模型运行尚未执行。

## 提交前验证

- 项目虚拟环境定向测试：60 passed；compile 和 OpenSpec strict 校验通过。
- 完整 `python -m scripts.dev smoke`：退出码 0，5284 passed、1 skipped、23 deselected、61 warnings；采用项目原有命令与选择配置，未新增跳过或削弱断言。
- 运行链路 smoke：`succeeded`，`network_calls=0`；source 校验：`is_valid=true`、error/warning 均为 0。
- 从本 JSON 独立复算 41 题的前后两组共 82 个评分，结果一致；验证题库仅删除 `single_007`，其余行逐字保留。
- 完整 smoke 日志的本地路径及 SHA-256 保存在 JSON 的 `verification` 中；验证完成时间采用北京时间。
