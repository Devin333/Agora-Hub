# Research 自适应执行：研究依据

- 文档日期：2026-10-15。
- 资料获取／复核日期：2026-10-15；R3 的日期指接收主 agent 已核验材料，非本次独立源码巡检。
- 定位：为 `research-adaptive-execution` 的 PRD、设计与验收取舍提供依据，不代表本项目已经实现，也不构成性能承诺。
- 本地依据：已读取根 `AGENTS.md` 与本变更 `proposal.md`；未进行仓库实现定位，不据此确认任何生产能力。
- 核验范围：R1、R2 复用前次全文研究并复核；R4、R5 复核原论文与关键算法；R3 保留主 agent 提供的源码核验边界。
- 引用规则：R1–R5 是稳定来源 ID；论文事实、源码观察与本项目工程推论分别表述，不引用性能提升数字。

## 范围与约束

- 研究问题是如何自动决定必要工作量，而不是让用户选择 `static` / `dynamic`、planner 或多 agent。
- 本变更首版不训练专用 router；论文中的训练算法是比较依据，不是本期实施任务。
- 适配范围遵循 `proposal.md`：既有 Research 分析与声明的补证范围，不扩大为任意联网、多论文通用研究或自动 skill 演进。
- 现有业务主链、Harness 控制权、统一质量标准、授权、请求级预算、发布和回放边界均不得因借鉴论文而弱化。

## 来源与机制对照

| ID | 机制 | 训练／执行前提 | 可以借鉴 | 不能照搬 |
| --- | --- | --- | --- | --- |
| R1 | Adaptive-RAG：解题前选择不检索、单次检索或多轮检索。 | 已训练问题分类器；基于标准答案与数据集先验生成标签。 | 用户不选策略；按任务分配不同工作量。 | 固定三选一不是任意 workflow；零检索不授权 Research 跳过证据链。 |
| R2 | MBA-RAG：把同样三种策略作为 bandit 的臂，学习质量与成本收益。 | 问题编码器、探索过程、正确性反馈与奖励配置。 | 同时考虑质量、调用和检索成本。 | 不是免训练路由；不能假定生产请求天然提供标准答案或可靠奖励。 |
| R3 | LangChain / DeepAgents：简单任务可不写 todo；模型按需调用 `task` 委派。 | 对应 middleware 已启用、工具已暴露，模型能够理解提示并调用工具。 | 规划与委派按需启用，而非每次强制发生。 | 提示偏好不等于硬约束；不能推断所有 DeepAgents profile 默认安装 todo。 |
| R4 | ADaPT：executor 先尝试，失败后才做有界递归 AND/OR 分解。 | 可执行的环境工具、固定提示和递归上限；原实验使用 LLM 自评驱动分解判断。 | 局部失败后按需增加工作，而非一开始规划所有细节。 | 自评不等于质量证明；外部验证器不是原论文已验证的替代方案。 |
| R5 | MasRouter：训练级联 router，选择协作模式、agent 数量、角色与模型。 | 预定义候选池、任务与标准答案、执行质量／成本反馈及训练。 | 将人数、角色和模型作为不同的内部资源选择维度。 | 不任意创造 workflow，不直接证明未训练规则策略有效，也不授权模型扩大能力边界。 |

## 原始证据与可复核定位

### R1 · Adaptive-RAG

- 原论文：`https://aclanthology.org/2024.naacl-long.389.pdf`；对应 `arXiv:2403.14403`。
- 作者项目：`https://github.com/starsuzi/Adaptive-RAG`；仅辅助确认训练与运行流程，不据此认定固定 release。
- 定位：§3.1–3.2，pp.7039–7040；§4.4，p.7041；图3，p.7042；Limitations，p.7044。
- `o = Classifier(q)` 的输入是问题文本；T5-Large 用交叉熵训练，不是在运行时根据已检索证据重新学习路线。
- 标签先看三策略能否答对：优先选答对的简单策略；全部失败时，以数据集单跳／多跳先验分别补为 B／C。
- “最简单”是预设策略优先级，不是逐请求测出的最低实际成本；分类误判和补标误差均不能忽略。

### R2 · MBA-RAG

- 原论文：`https://arxiv.org/pdf/2412.01572v4`；此处固定引用 v4，不把无版本链接当作永不变化的正文。
- 定位：§2.2–2.3，PDF 第2–3页；附录A.2，PDF 第6页。
- DistilBERT 编码问题，使用 epsilon-greedy 探索三个臂；训练目标是所选臂奖励预测的平方误差。
- 正文奖励为 `r_a = A(y, y_hat_a) - lambda * C(a)`；质量项依赖标准答案，成本可用检索步数表达。
- 附录另按单跳／多跳手设奖励：单跳为 `1 / 0.9 / (1-step/10)`，多跳为 `4.3 / 2.3 / 1.15`；这是实验配置，不是通用奖励常数。
- 因此不能声称无需人工先验、可对无标准答案的业务请求自动在线学会最优路线；R1、R2 的开放域 QA 证据也不等同于长报告研究的迁移验证。

### R3 · LangChain / DeepAgents 按需工具使用

- `TodoListMiddleware` 来源：`https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/langchain_v1/langchain/agents/middleware/todo.py`。
- `SubAgentMiddleware` 来源：`https://raw.githubusercontent.com/langchain-ai/deepagents/main/libs/deepagents/deepagents/middleware/subagents.py`。
- 核验归属：上述源码结论由主 agent 提供；本文件只承接“简单任务不必 `write_todos`”与“模型按需调用 `task`”两项结论。
- 定位：对应 middleware 的工具说明与提示内容；这说明工具被注册后的使用方式，不足以证明任何入口或 profile 的默认装配。
- 两个 URL 分别跟随 `master` / `main`，未锁定 commit，不是固定 release；实施前应由负责实现核验的角色补充版本、入口和装配证据。
- todo 是规划／进度表达，`task` 是委派入口；两者都不能自动成为本项目的授权、预算或质量判定依据。

### R4 · ADaPT

- 原论文：`https://aclanthology.org/2024.findings-naacl.264.pdf`。
- 定位：§3.1–3.3，pp.4228–4230；Algorithm 1，p.4239／PDF 第14页；附录F及图12，p.4243／PDF 第18页；Limitations，p.4235。
- Algorithm 1 先检查深度上限，再调用 executor；仅当 `completed` 为假时调用 planner，递归执行子任务并组合 AND/OR 结果。
- 关键限制：`completed` 主要来自 executor LLM 的成功自评；附录F用环境金标准对照，发现不一致，尤其可能因高估成功而提前停止。
- 环境回报用于对照评估，不应误写成原运行机制已经用外部验证器替代自评。
- Limitations 将外部验证器列为未来方向；本项目坚持 deterministic gate 是独立工程约束，效果必须重新验证，不能借用论文成绩作担保。

### R5 · MasRouter

- 原论文：`https://aclanthology.org/2025.acl-long.757.pdf`。
- 定位：§3.1–3.2，pp.15551–15552；§4.1–4.4，pp.15552–15554；附录B的 Algorithm 1，p.15563／PDF 第15页。
- 搜索空间显式限定为模型池 `M`、角色集 `R`、协作模式集 `T`；级联选择模式、数量、角色和模型，再执行所选组合。
- Algorithm 1 根据质量减成本的奖励更新参数；agent 数量由学习到的复杂度映射与人工设定上限共同约束。
- 训练期对候选组合的探索不等于运行时任意发明图结构；将此用于 NewsRoom 仍需合法候选注册、能力匹配及自身评测。
- §3.2 的成本包含调用、API 或 token 成本；不是用用户问题字数或最终答案长度替代真实执行开销。

## 对本项目的工程推论

以下是结合上述资料与 `proposal.md` 的设计取舍，不是外部论文已经验证的 NewsRoom 方案。

### 自动适应不等于用户选模式

- 用户提交目标、材料与约束；固定 worker、受限 AgentLoop、受控 TaskPlan 是内部实现选择，不要求用户理解路由术语。
- 借鉴 R1、R3 的按需使用，不推导“每个请求必须先多调用一次 planner”；已知任务可以直接走既定分析路径。

### 候选不等于控制权

- 模型可提出意图、拆解、补证或委派候选；Harness 根据声明能力、输入事实、授权和剩余预算接纳或拒绝。
- routing、quality pass/fail、memory write、tool authorization 与 publication 仍由 Harness／对应确定性服务负责。
- R4 的自评只能作为候选信号，不能直接放行；`PLAN -> EXECUTE -> VERIFY` 的转换及拒绝原因必须持久记录。

### 简单任务仍保留质量

- 简单表示不增加无必要的规划或调用，不表示降低用户合同、证据覆盖、必需角色输出、引用要求或 quality profile。
- R1 的零检索分支不可移植为跳过 `source collection -> evidence -> agent analysis -> report -> quality gate -> artifacts/storage`。
- 确定性门可检查结构、引用与预算约束，但不自动证明全部语义结论正确；语义质量仍需留出评测与审阅。

### 执行成本不是字数

- 借鉴 R2、R5 的成本视角，记录 planner／worker 调用、输入输出 token、检索／工具开销、重试及请求总延迟。
- 问题或答案更短不能单独证明更省；并行缩短墙钟时间也不能单独证明总 token 或费用更低。
- 路由、失败尝试和升级的开销均计入同一请求预算，不在切换策略时清零，也不以省成本为理由绕过质量门。

## 实施顺序、验证与完成标准

1. 先冻结业务范围、合法候选、共用质量合同与预算边界；首版不因 R1、R2、R5 的训练机制新增 router 训练任务。
2. 再实现服务端接纳与局部升级；只对已接纳的缺证或可修复失败补工作，权限拒绝、取消、状态损坏和预算耗尽不得靠升级绕过。
3. 补齐 `max_replans`、`max_turns`、retry budget 与持久事件；依赖资格未满足时不启用自适应 release，不改写活动 run 的冻结 identity。
4. 用同一留出任务、质量合同与成本口径，对比固定路径、自适应路径和始终规划路径；纳入误判、失败恢复、预算耗尽及跨域任务。
5. 只有本项目成对评测与发布门通过，才可宣称质量与成本目标达成；数值阈值由 PRD／评测合同另行确定，不能抄用论文结果。

## 交接与验收边界

- 本文完成标准：R1–R5 可追溯，版本与获取日期明确，事实和工程推论分离，没有性能数字承诺或默认装配外推。
- 实现与测试须交由主 agent 按角色边界分派；本文不修改代码、不标记实现任务完成，也不变更生产默认行为。
- 全变更材料汇齐后，由主 agent 统一执行 `openspec validate research-adaptive-execution --strict` 并负责集成提交。
- 需要保留的差异：专用 router 不在首版范围；R3 主分支证据待锁版本；R4 外部验证替代方案需要独立验收；R1 的无检索策略不适用于取消必需证据链。
