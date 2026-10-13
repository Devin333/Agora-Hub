## ADDED Requirements

### Requirement: Formal evaluation uses the existing structured core

正式 RAG 实验 SHALL 通过既有 evidence evaluation core 的版本化结构化 options 执行，至少声明 dataset/corpus/experiment 身份、track、execution profile、variant、预算、输出及恢复方式。CLI/application service SHALL 复用该 core，不建立第二套 evaluator，也不让库调用方拼接 CLI argv。

#### Scenario: Formal command delegates through application service
- **WHEN** 用户调用拟新增的 `python -m scripts.dev rag-eval run --manifest` 并提供合法实验配置
- **THEN** 入口通过 application service/composition 构建结构化 options 与端口后调用既有 core
- **AND** configuration fingerprint 与实际装配的 component provenance 一起记录

#### Scenario: Library caller executes an experiment
- **WHEN** 库调用方提供正式 options 与已注入端口
- **THEN** 它无需调用另一个 CLI 或构造 argv 字符串即可运行相同协议

### Requirement: Production adapters receive only blind inputs

正式 `retrieval` 与 `end_to_end` application adapter SHALL 仅接收 case ID、问题、合法 actor/source scope 和预声明运行配置。core MUST NOT 向其传入完整 `EvidenceQAPair`、reference labels、gold ID、期望拒答标签或可读答案册的回调。端口 SHALL 在 Research 定义并由外层 composition 装配，Research MUST NOT 反向导入 interfaces 或 infrastructure。

#### Scenario: Reference-bearing data reaches the adapter boundary
- **WHEN** 正式 A/C 请求试图携带 gold context 或参考答案字段
- **THEN** 输入合同在生产调用前拒绝该请求并给出字段/原因诊断
- **AND** 不调用 provider 或将其静默改称成功的生产实验

#### Scenario: Production end-to-end uses the gated product path
- **WHEN** 正式端到端 adapter 运行 chunk RAG
- **THEN** 它调用实际 application service 的 gated Harness 回答路径
- **AND** 不通过 direct answer generator 绕过现有 gate、预算或 transcript

### Requirement: Dataset preparation is separate from formal execution

题库生成、标注与冻结 SHALL 独立于正式 run。正式模式 SHALL 只读取已冻结、经审核且哈希匹配的题库，不根据当前待测 chunks 自动建题或回填 gold。source-to-chunk 映射 SHALL 作为绑定索引版本的派生产物验证，不能修改冻结原文标签。

#### Scenario: Formal run requests automatic golden-set generation
- **WHEN** 一个正式 manifest 同时要求生产评测和按待测 chunks 重建问题
- **THEN** preflight 拒绝该配置
- **AND** 用户只能通过独立候选制作/fixture 操作完成出题后再审核冻结

### Requirement: Existing diagnostic commands remain explicitly classified

现有 evidence/live/CI 命令 SHALL 保留既有参数、诊断返回码和基础结果字段；本变更明确允许这组兼容接口。新输出 MUST 标识协议、track/profile、实际 adapter 及 gold 使用情况。真实模型或外部语料 SHALL NOT 单独赋予 production 身份，未选择正式协议的既有调用 SHALL 保持 diagnostic/regression 用途。

#### Scenario: Existing live command uses external papers and hash embedding
- **WHEN** 既有 `run-live-answer-eval` 读取外部论文并调用真实模型，但检索由确定性 hash adapter 执行
- **THEN** 既有命令行为与诊断字段保持可用
- **AND** 报告明确其非 production 执行身份，不进入正式端到端比较

### Requirement: Formal CLI provides bounded preparation and replay operations

拟新增的 `rag-eval` 命令组 SHALL 提供 `validate-dataset`、`run`、`score`、`export-review`、`import-review`、`report` 子操作，并全部通过 application service 和结构化 core/工件端口执行。`score` 和 `report` MUST NOT 隐式重跑检索或回答模型。`run` SHALL 在付费操作前校验冻结数据、完整语料、端口身份与预算。

#### Scenario: Re-score an existing immutable run
- **WHEN** 用户通过 `score` 指定已完成的原始运行和一个新的评分 profile
- **THEN** 系统追加新的 score revision 并复用原始响应
- **AND** 不重新执行 RAG、不覆盖旧评分或原生 transcript
