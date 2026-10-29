## 1. 查询向量

- [x] 1.1 新增端口 `QueryEmbeddingPort` 与 `QueryVectorStorePort`，参数和返回值只使用标准库类型
- [x] 1.2 实现 `QueryVectorResolver`，按基础缓存 → 补充缓存 → 实时调用的顺序查找；校验缓存身份，对向量做归一化，并统计每一层的命中与调用次数
- [x] 1.3 fixture store 与字段索引增加可选参数 `query_vector`，不传时保持原有的严格离线行为
- [x] 1.4 infrastructure 层实现 npz 补充缓存，写入采用原子操作

## 2. 评分

- [x] 2.1 实现与官方脚本口径一致的 Answer-F1 及预测投影，并用官方 `qasper_evaluator.py` 交叉验证结果
- [x] 2.2 实现分层确定性抽样，并生成抽样清单
- [x] 2.3 基于冻结映射计算 strict 与 equivalent 两种口径的引用覆盖和上下文覆盖
- [x] 2.4 实现 judge：构造 prompt、严格解析 JSON、处理 judge_error

## 3. 编排与入口

- [x] 3.1 在 application 层实现 `QasperAnswerEvaluationService`，组装生产回答 session，逐题运行、评分并写出结果文件
- [x] 3.2 在 composition 层注入 embedding 模型、回答模型和 judge
- [x] 3.3 新增 CLI `rag-eval qasper answer run`

## 4. 验证

- [x] 4.1 单元测试：解析器、投影与 F1、抽样、覆盖率、judge 解析、编排（使用假模型）
- [x] 4.2 小样本真实运行（每类 2 题），确认链路完整打通，embedding 补算结果已写入补充缓存
- [x] 4.2a 修正框架上下文预算把 evidence_memory 承载段计为一条证据的错误（满 8 条证据时被误判超限）
- [x] 4.3 `openspec validate qasper-end-to-end-answer-evaluation --strict`
- [x] 4.4 `python -m scripts.dev smoke`
- [x] 4.5 提交
- [x] 4.6 300 题完整真实运行，并整理证据摘要（见 evidence.md）
- [x] 4.7 导出 50 题 judge 人工校准集，并提供标签评分命令
- [ ] 4.8 人工填写校准标签并评分，确认 judge 可信度
