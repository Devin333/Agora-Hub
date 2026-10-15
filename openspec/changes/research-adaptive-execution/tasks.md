本文是后续实施清单；所有任务保持未完成。本次 PRD/OpenSpec 文档交付不代表代码、真实模型评测或生产发布完成。实施中遵守根 AGENTS.md，每次代码提交前运行相应检查及必需 smoke gate。

## 1. 合同、依赖与评测基线

- [ ] 1.1 核对实际代码及 runtime-parallel、evidence-quality、desktop-readiness、desktop-app 的 owner/发布门，记录可接入能力和未满足条件，不改写其他变更的完成状态。
- [ ] 1.2 冻结首版单论文 scope、交付角色、同源修订边界、任务意图候选及公开请求 schema，区分搜索意图、信息不足和能力不可用。
- [ ] 1.3 定义版本化 execution policy、独立 quality profile、接纳/拒绝原因码及共享预算归属；明确允许策略/binding 集合与可变化的 plan revision。
- [ ] 1.4 在既有评测基础中建立固定、始终规划、自适应三组可比较配置与任务分层，设计论文/任务族隔离及独立语义 rubric。

## 2. 无模式入口与低开销准入

- [ ] 2.1 在 Research application 中实现已知结构化请求的确定性规范化；证明标准任务不新增语义分类/planner 调用。
- [ ] 2.2 接入真实结构化候选 adapter，执行自然语言任务意图校验、授权 ref 解析和有界必要澄清；禁止重复复杂度分类流程。
- [ ] 2.3 实现服务端 release/profile admission；将任务需求候选转换成可审计 policy decision，而非直接消费模型路由字段。
- [ ] 2.4 更新公开 analyze 请求合同与 validation，明确拒绝 legacy mode/internal override 字段；与正式合同切换同步启用，不提前破坏现有生产默认。
- [ ] 2.5 补齐结构化/自然语言/缺信息/不支持任务/模式注入的 application 与 API 合同测试，对应 AC-01、AC-06、AC-07。

## 3. 固定 Graph 内的按需执行

- [ ] 3.1 定义并注册版本化自适应 Graph，声明粗粒度 Choice、bounded loop/repair 与固定前后缀；复用现有 Graph controls，不新增 worker kind 或调度器。
- [ ] 3.2 将原固定 worker 路径接入统一分析阶段，保持必需角色、既有输入输出与零额外 planner/fan-out 行为。
- [ ] 3.3 在已注册同论文能力内接入真实 bounded AgentLoop，明确输入、工具、输出、gate、预算和终止条件。
- [ ] 3.4 接入受控 TaskPlan 候选校验、exact capability/binding、必要角色及确定性聚合，支持必要时初始采用该路径而非先整条失败重跑。
- [ ] 3.5 覆盖所有策略的 Graph identity 不变、越权/跨阶段任务拒绝、必要角色与无效 fan-out 合同测试，对应 AC-02、AC-05、AC-14。

## 4. 局部修复与质量解耦

- [ ] 4.1 定义可驱动适配的 accepted observation/gate refs，与 UI progress、todo、模型自评区分，明确可修复/不可升级错误分类。
- [ ] 4.2 实现同论文、同源修订的真实补证 adapter 与声明 repair 边界；形成新版本 refs、血缘及受影响结果再验证，不原地改写 accepted 输入。
- [ ] 4.3 实现失败指纹、新事实检查和局部 replan 限制；复用有效结果，阻止同一失败无证据重复升级或全 run 无条件重跑。
- [ ] 4.4 解除 quality profile 与 dynamic flags 的耦合，确保固定、loop、TaskPlan 共用适用证据门和顶层 publication authority。
- [ ] 4.5 添加缺证修复、陈旧结果失效、假成功、硬失败不升级、重复指纹与同质量跨路径测试，对应 AC-03、AC-04、AC-09、AC-12。

## 5. 共享预算与持久恢复

- [ ] 5.1 把意图解释、planning、worker/tool、child、retry、compaction 与恢复纳入既有请求级预算 owner，定义澄清和授权扩额的 revision 语义。
- [ ] 5.2 接通派发前原子 reservation、receipt 结算和未知结果保守占用，覆盖并发竞争及禁止策略切换重置额度。
- [ ] 5.3 扩展既有 canonical event/transcript 合同，记录候选、接纳/拒绝、理由、版本、refs、预算和 plan revision，保证接纳持久化先于派发。
- [ ] 5.4 实现 exact identity 恢复、pending child/旧 revision receipt 处理与幂等聚合，禁止重启后重新分类或偷偷更换 Graph。
- [ ] 5.5 用故障注入覆盖接纳前后、派发前后、receipt/聚合/发布前后 crash、乱序/重复事件、预算耗尽与取消，对应 AC-08、AC-10、AC-11。

## 6. 用户进度与部署边界

- [ ] 6.1 更新现有 Desktop analyze 入口与状态 projection，移除技术模式选择，只展示材料、分析、补证、核验、澄清和真实终态。
- [ ] 6.2 提供事实性升级/停止原因、资源消耗与缺口说明，复用取消和必要授权，不暴露内部推理、私有材料或凭据。
- [ ] 6.3 实现按合格 task/quality/model/capability profile 的 rollout admission，显式区分 disabled、unavailable 与已验收 serial；缺失依赖不得隐藏降级。
- [ ] 6.4 实现只影响新准入的 rollback，保留活动/历史 run 的 exact 恢复绑定；清理新准入中的旧模式分支与无必要兼容代码。
- [ ] 6.5 补齐无模式 UI/API、取消与故障真实终态、依赖缺失、rollback 不降质量等集成测试，对应 AC-01、AC-06、AC-11、AC-13。

## 7. 产品验收与交付

- [ ] 7.1 建立 AC-01 至 AC-14 的需求—实现—测试证据映射，完成授权/注入/引用血缘/预算/恢复/发布等确定性控制矩阵；不得以 fake 结果证明真实语义收益。
- [ ] 7.2 在明确授权的资源内进行 pilot，依据方差冻结样本量、统计方法、配对重复、价格版本和发布阈值；正式留出集不得用于调参。
- [ ] 7.3 执行受控真实模型配对留出评测，记录完整失败分母、独立语义判定、所有调用/token/检索/延迟及置信区间，按 PRD 9.3 作通过或阻塞结论。
- [ ] 7.4 核验所有适用上游 release gates 与本变更产品门，完成 scope 灰度/回滚演练；证据不足保持未发布，不硬编码绕过或降低门。
- [ ] 7.5 更新用户说明与实现状态；执行 `openspec validate research-adaptive-execution --strict`、范围匹配测试、`python -m scripts.dev compile` 和必需 `python -m scripts.dev smoke`，修复根因后仅提交本变更代码与证据。
