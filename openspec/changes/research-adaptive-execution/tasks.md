本文是后续实施清单；所有任务保持未完成。本次 PRD/OpenSpec 文档交付不代表代码、真实模型评测或生产发布完成。实施中遵守根 AGENTS.md，每次代码提交前运行相应检查及必需 smoke gate。

受控多轮专项依据 `analysis-agent-loop-prd.md`、`specs/research-controlled-analysis-loop/spec.md` 及评测规范，优先实施实验分析。第 8 节是 `3.3` 的可执行细项，`3.3` 只承担集成验收，不重复实现；涉及第 4–7 节共同能力时复用同一实现与证据，专项通过不能自动完成覆盖范围更大的父任务。实施顺序为适用 `1.x` 合同冻结 → `8.1–8.11` 控制闭环 → 获授权后 `8.12–8.14` 语义与资格验收；此顺序不提前切换公共 API 或启用生产默认。

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
- [ ] 3.3 对第 8 节实验分析 bounded AgentLoop 做集成验收，核对真实模型/工具接线、证据接纳、输出/gate、共享预算和停止条件；保留其他必需角色，不新增 planner。仅在对应控制和资格证据完整后标记该项完成。
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

## 8. 实验分析受控多轮专项（细化 3.3）

- [ ] 8.1 冻结实验分析任务/输出、同论文同修订输入与只读工具范围，确认适用 Graph-bound 入口、owner 和依赖资格；登记有限的 task/model profile 与累计预算口径，禁止新增 planner、checkpoint 或递归委派。对应 AL-FR-01、AL-FR-07、AL-FR-10。
- [ ] 8.2 将真实 LLM client 接入动作协议，区分中间 action 与最终业务 schema；实现并测试结构化动作或原生 tool_calls 的明确适配，透传真实 usage、错误与 execution identity，不能继续以固定 final_output 包装证明完成。对应 AL-FR-02、AL-AC-03。
- [ ] 8.3 注册有执行身份与权限约束的真实 retrieval.read_source adapter，接入现有材料 service，返回来源/修订/位置/结果 refs，限制结果和上下文大小；覆盖无权来源、未解析材料与提示注入。对应 AL-FR-03、AL-AC-04。
- [ ] 8.4 结合 4.2 接通补读 observation 到既有 evidence/ref authority 的接纳，形成不可变新版本与派生血缘，禁止模型自建合法引用；覆盖未接纳引用拒绝、旧输入不变和受影响结果再验证。对应 AL-FR-04、AL-AC-05、AL-AC-06。
- [ ] 8.5 在既有 Graph-bound worker 路径组装实验 loop，将实际工具观察回填下一轮并适配既有实验候选输出；保留其他角色及主链，验证首轮可完成且局部校验不能替代 VERIFY/发布。对应 AL-FR-01、AL-FR-05、AL-AC-01、AL-AC-02、AL-AC-12。
- [ ] 8.6 结合 4.1、4.3 接通结构化反馈、gate/observation 事实和声明 repair，区分本地格式修复、业务缺证、传输故障与硬失败；实现失败指纹及无新事实停止，拒绝借换工具/路径/委派继续。对应 AL-FR-06、AL-AC-07、AL-AC-08。
- [ ] 8.7 结合 5.1、5.2 验证该模型/工具路径的真实调用、provider/tool retry、repair 和未知结果进入同一请求 ledger，派发前预留并按 receipt 结算；覆盖与其他分析分支竞争、重入不清零和累计上限。对应 AL-FR-07、AL-AC-09。
- [ ] 8.8 结合 5.3、5.4 接通 phase/turn、动作、工具/证据 receipts、候选/gate 和停止事实；区分纯回放与恢复后新执行，保持版本/身份，隔离并行上下文，处理取消与迟到结果。对应 AL-FR-08、AL-AC-10、AL-AC-11。
- [ ] 8.9 结合 6.1、6.2 完成既有用户状态投影：区分分析、补证、核验、证据不足、耗尽、不可用和取消；局部候选不显示总体完成，限制性交付遵守既有 quality profile，不新增技术模式选择。对应 AL-FR-09、AL-AC-12。
- [ ] 8.10 建立经过生产 application/composition、Graph-bound worker、真实工具 adapter 与 gate 的离线 fixture 集成测试，验证工具调用→观察回填→合法候选及首轮直接完成；缺工具或仅 final_output 包装必须不能伪通过。对应 AL-AC-01 至 AL-AC-06、AL-AC-12。
- [ ] 8.11 完成专项权限/注入/无进展/预算竞争/隔离矩阵和故障注入：预留、派发、工具返回/receipt、证据接纳、gate、发布及取消边界；断言无越权、无重复结算/发布、无预算归零及未知状态不盲目重发。对应 AL-AC-04、AL-AC-06 至 AL-AC-12。
- [ ] 8.12 结合 7.2 为一次生成与受控 loop 两组注册专项评测协议，冻结材料/范围/模型/价格/质量/预算、论文级隔离、独立 rubric、完整分母与统计方案；取得明确资源授权后才执行 pilot，正式留出前冻结参数。对应 AL-AC-13、评测规范。
- [ ] 8.13 在批准资源内完成正式模型动作/usage 契约和配对留出实验，记录合法补证轨迹、独立语义质量、失败、全部资源与规定任务边界延迟；按专项门记录通过或不足，不冒充父级三组/-2个百分点/15%门已通过。对应 AL-AC-03、AL-AC-13、AL-AC-16。
- [ ] 8.14 结合 6.3–6.5、7.1、7.4 验收现有模型配置无额外 checkpoint 的部署、缺失依赖拒绝及只影响新准入的回滚；完成下表的规范—实现—测试/运行证据映射和资格审查，保持总体未满足的门及任务未完成。对应 AL-FR-10、AL-AC-14 至 AL-AC-16。

### 专项验收追踪索引

表中是实施入口与证据要求，不是已完成记录；正式通过仍需绑定具体代码、测试结果、运行/评测 receipts 和适用版本。

| 验收编号 | 规范入口 | 实施任务 | 必需证据 |
| --- | --- | --- | --- |
| AL-AC-01 | controlled-analysis-loop 的任务/低开销要求；evaluation 专项轨迹要求 | 8.1、8.5、8.10 | 无新增 planner/fan-out、可首轮提交、其他角色保留 |
| AL-AC-02 | controlled-analysis-loop 的动作、工具、观察与输出要求 | 8.2–8.5、8.10 | 生产接线上的工具执行、下一轮输入、候选与 gate |
| AL-AC-03 | controlled-analysis-loop 的真实模型协议；evaluation 专项证据要求 | 8.2、8.13 | 动作适配、usage、identity、错误；分别标注 fake 和真实模型 |
| AL-AC-04 | controlled-analysis-loop 的作用域与只读工具要求 | 8.3、8.11 | 未授权来源/工具/修订及材料注入拒绝 |
| AL-AC-05 | controlled-analysis-loop 的证据接纳与版本要求 | 8.4、8.10 | 未接纳引用拒绝、合法派生 refs 可回溯 |
| AL-AC-06 | controlled-analysis-loop 的依赖失效与重验要求 | 8.4、8.10、8.11 | 旧输入不可变、受影响结果失效和未影响结果复用 |
| AL-AC-07 | controlled-analysis-loop 的反馈与无进展停止要求 | 8.6、8.11 | 失败事实、声明 repair、指纹及停止日志 |
| AL-AC-08 | controlled-analysis-loop 的硬失败边界要求 | 8.6、8.11 | 硬失败后没有越权升级或新派发 |
| AL-AC-09 | controlled-analysis-loop 的预算与真实调用计量要求 | 8.7、8.11 | 请求 ledger、原子预留、重试/恢复/未知结果消费 |
| AL-AC-10 | controlled-analysis-loop 的持久记录与恢复要求 | 8.8、8.11 | crash/replay/继续执行、receipt 核对和幂等证据 |
| AL-AC-11 | controlled-analysis-loop 的执行身份与隔离要求 | 8.8、8.11 | 并发与复用后端时不串输入、结果、权限或预算 |
| AL-AC-12 | controlled-analysis-loop 的三级验收与诚实状态要求 | 8.5、8.9–8.11 | loop 成功而 gate 失败时无总体成功/发布 |
| AL-AC-13 | evaluation 的专项轨迹与局部资格要求 | 8.12、8.13 | 独立语义、完整失败分母、全部成本与合法补足案例 |
| AL-AC-14 | controlled-analysis-loop 的依赖/回滚要求；evaluation 资格边界 | 8.14 | 缺失资格拒绝、新准入回滚、旧 identity 不变 |
| AL-AC-15 | controlled-analysis-loop 的现有模型部署要求 | 8.14 | 部署 smoke、依赖清单和无额外 checkpoint |
| AL-AC-16 | evaluation 的专项与总体发布分离要求 | 8.13、8.14 | 全部映射、独立状态及未满足总体门的明确记录 |

上述 `controlled-analysis-loop` 均指 `specs/research-controlled-analysis-loop/spec.md`；`evaluation` 指 `specs/research-adaptive-execution-evaluation/spec.md`，与原任务使用同一 OpenSpec change。
