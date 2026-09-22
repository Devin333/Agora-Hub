# Research Claim Review Stage 1 Evidence

日期：2026-09-22

## 已交付范围

本阶段只实现 Research domain 的不可变评审合同和纯确定性 resolver，对应任务 2.1、2.2。新增合同包括 `ClaimRevision`、`ReviewBinding`、`EvidenceRequirement`、`CheckObservation`、`ReviewObservation`、`DisputeCase` 和 `ResolutionRecord`。合同冻结 claim revision、paper scope、evidence checksum、规则版本和独立复核策略；`to_dict()`/`from_dict()` 会校验派生 checksum，未知字段和跨绑定输入不会被静默接受。

`ClaimReviewResolver` 只消费冻结 binding、requirements、checks、observations 和持久化案卷输入，不调用模型、来源、时钟或 store。`UNAVAILABLE`/`INPUT_INVALID` 检查只能产生 `VERIFICATION_UNAVAILABLE`；缺少必要材料、同范围支持/反驳冲突、缺少实验型独立人工复核分别保持 `INSUFFICIENT_EVIDENCE`、`DISPUTED` 和 `PENDING`。Citation check PASS 不会单独授予语义支持。

## 验证命令

```text
E:/Anaconda3/python.exe -m pytest tests/backend/research/domain/test_claim_review.py tests/backend/research/services/test_claim_review_resolver.py tests/backend/research/domain/test_models.py tests/backend/research/services/test_quality_gate.py -q
```

结果：`17 passed`。

```text
E:/Anaconda3/python.exe -m compileall -q backend/research/domain backend/research/services
openspec validate research-evidence-review-resolution --strict
```

结果：编译通过；OpenSpec 严格校验通过。

## 明确未完成

本证据不声明任务 2.3/2.4、3.1-3.4、4-9 已完成。当前没有真实 review persistence migration、CAS/outbox、application service、Graph/UI 接入、规则候选发布、真实来源评测、影子/强制启用或完整项目 smoke 证据；现有 `CitationVerifier`、`ResearchQualityGate` 和正式报告发布护栏保持原语义。
