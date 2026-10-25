## Why

2026-10-25 的 QASPER 300 题运行中，引用支持率 micro 为 174/213（81.69%）；去掉 45 道生成 502 后，旧口径正确率为 125/233（53.65%，另排除 22 道 judge 错误）。调查已确认生成正文被摘要前缀替代、生成与判引用使用的文本不一致，以及整答单 claim 的结构校验无法识别语义错引；需要按可复验的阶段修复，并在独立样本上验证引用支持率至少 98%，同时提高回答正确性、减少误拒。

## What Changes

- 将完整证据正文与摘要分开传递，在最终请求处执行真实预算，持久化每次生成实际可见的文本、编号、来源偏移和内容身份。
- 引入内部原子 claim 与精确引用绑定协议，由确定性 renderer 生成对外答案，取消整答自动挂载全部 evidence/span 的降级放行。
- 扩展结构 gate、独立且经人工校准的语义观测及 Harness 有界修复；LLM 只提供候选和观测，不能决定通过、路由、重试次数或发布。
- 复用现有检索 profiles、离线 replay、judge 恢复与人工校准工具，分离投影、生成、检索和 provider 故障的影响；先小样本对照，再进行独立论文族验收及完整 300 题回归。
- 冻结准确率、引用支持 micro/macro、核心事实覆盖、正确实答产出、误拒及故障的公式、阈值和分母；阻止以增加拒答、删除关键事实、丢弃未评分项或反复择优重答达到 98%。
- 新内部候选协议明确扩展 `qasper-answer-observability-and-grounding` 的旧范围限制；对外文本答案和引用字段保持兼容，历史工件保持不可变，新行为与旧故障恢复分别标识。

## Capabilities

### New Capabilities

- `paper-answer-quality-validation`：可复验的回答质量基线、人工校准、分阶段对照、故障诊断及独立联合验收。

### Modified Capabilities

- `rag-harness-kernel-evidence-adapter`：完整正文与 summary 分离，跨适配器保留内容身份和来源。
- `rag-kernel-generation-contracts`：内部结构化候选、最终模型可见上下文快照及真实请求预算合同。
- `rag-answer-span-citation-gate`：原子事实、精确可见引用、回答覆盖及语义观察与结构证明的区别。
- `rag-generation-answer-gate`：按冻结 policy 处理独立观测，由 Harness 控制回答验证、结果和持久化。
- `rag-answer-supplemental-round`：将合法 ID 下的支持缺口纳入现有有界修复路径，保留默认两次生成上限。

## Impact

- Framework：`framework/harness/rag/` 的 evidence、context、answer gate、session 合同，以及 `framework/rag/` 的通用生成合同；不引入 Research 依赖。
- Research：`backend/research/rag/adapters/`、回答生成器及 QASPER evaluation/application；composition 仅负责 provider 与 policy 组装，CLI 仅调用 application service。
- 复用已完成的 observability、retrieval-recall、faithfulness calibration 资产；现有人工校准待办仍是质量结论的前置条件，不将未完成的全量 evaluation protocol 视为已交付能力。
- 实施需要范围匹配测试、`python -m scripts.dev smoke`、OpenSpec strict validation 和提交；本次提案不修改产品代码、不启动付费模型实验、不切换生产默认策略。
