# 第 7 节：最终验证与交付记录

日期：2026-10-10。对应 `tasks.md` 7.1–7.15。本轮无条件交付已验收。条件任务 6.6–6.12 未触发，保持待办；本记录和受测代码共同提交。

## 本轮实现边界

按章节完成全部实现和测试代码，再集中测试、修复和验收；第 7 节运行全量回归和提交门禁。第 1–6 节的详细记录见本目录各章证据。

- 输入合同由 Research application service 所有。profile、actor/source/revision 和可信资格验证后生成不可变投影；沿 TaskPlan、child runner、AgentRunner、AgentLoop、candidate worker 到实际 provider 逻辑请求保留受保护输入。首次、工具后续及重试请求均验证，不完整时 fail closed。
- V1 限定为 `numeric_comparison_result_v1`：同一真实表格中、相同 Dataset/Split 条件下两个对象的明确 accuracy 百分比比较。单位和缩放必须由表头或显式 Unit/Scale 提供，不根据数值大小猜测。确定性 resolver/gate 核对实际接受的来源、locator、修订、checksum、Claim 和报告投影；仅 supported 形成资格。
- 使用精确 `research.paper_analysis.dynamic.quality-foundation.graph@1` 和 `ResearchClaimEvidenceEligibilityGate@1`；默认 Graph 的 gate inventory 保留既有合同，registry 可解析新旧精确引用。运行身份贯穿 context、memory admission、结果提交和 recovery。
- 默认关闭的候选试验只允许有界受控扩展。严格预算、硬约束、范围及控制字段验证失败时执行预先计算的原始确定性计划；worker 不得伪造 fallback。配置和 lineage 可序列化。
- 进度记录是数值、不可变、有 checksum 的观察，沿既有 gate event/trace 持久化，不拥有路由、发布、停机或记忆权限。
- 修复真实 Windows 路径长度触发的 transcript staging 失败，保留原有原子提交与安全校验；OS 错误保留有界且脱敏的 cause chain。缺证路径能实际到达资格 gate，而非在 child transcript 提交前提前失败。

## 执行环境与实际检查

- 工作目录：`F:/github/NewsRoom`。
- Python：`3.14.4 (tags/v3.14.4:23116f9, Apr  7 2026, 14:10:54) [MSC v.1944 64 bit (AMD64)]`。
- 平台：`Windows-10-10.0.19045-SP0`。
- 命令入口使用仓库既有 `scripts.dev`；固定评测仅使用本地固定语料和测试 worker/transport，不要求外部 source/model 服务。
- 全量命令运行仓库默认 pytest 选集：`pyproject.toml` 既有 `not live_research_e2e` 排除需显式凭据的线上端到端测试；Redis、数据库等集成测试也保留既有 opt-in 条件。本轮没有修改这些选择规则，也没有声称执行了线上服务测试。

| 检查 | 实际结果 | 本地原始证据 |
| --- | --- | --- |
| `python -m scripts.dev compile` | 通过，退出码 0 | `.newsroom/quality-foundation/chapter-7-compile.log` |
| `python -m scripts.dev test-rag-eval-gate` | 通过，退出码 0；54 固定案例全部通过 | `.newsroom/quality-foundation/chapter-7-final-fixed-eval.log`、本目录 `fixed-quality-report-20261010.json` |
| `python -m scripts.dev test` 修复后最终全量 | 通过，退出码 0；8,966 通过、115 既有跳过、24 deselected、561 既有警告；5,550.24 秒 | `.newsroom/quality-foundation/chapter-7-final-full-tests.log`、`chapter-7-final-full-tests.xml` |
| `python -m scripts.dev test` 首轮 | 8,949 通过、2 失败、115 跳过、24 deselected；4,857.19 秒。两项并发根因已修复，受影响复验与最终全量均通过 | `.newsroom/quality-foundation/chapter-7-full-tests.log`、`chapter-7-full-tests.xml` |
| `python -m scripts.dev smoke` | 通过，退出码 0；4,188 测试通过、23 deselected、31 既有警告，4,269.66 秒；离线 AgentLoop 和 source validation 均通过 | `.newsroom/quality-foundation/chapter-7-smoke.log`、`chapter-7-smoke.xml` |
| `openspec validate research-evidence-context-quality-foundation --strict` | 通过，退出码 0；最终文档严格校验 | `.newsroom/quality-foundation/chapter-7-openspec.log` |
| diff、引用和提交范围 | diff、文档引用与范围核对通过；证据显式加入，staged diff 通过后提交 | `.newsroom/quality-foundation/chapter-7-diff-check.log` |

全量结果与 smoke 结果按各自实际执行计数记录，不将分章通过数累加为最终全量通过数。跳过项需保留既有条件及真实原因，不新增 skip 或放宽断言以绕过失败。

## 最终全量结果

全部代码和回归测试完成后，修复后的 `python -m scripts.dev test` 再次从头完整执行默认选集，**8,966 通过、115 既有跳过、24 deselected、0 失败，561 既有警告，5,550.24 秒（1:32:30）**。pytest summary 耗时与 JUnit suite 的 5,548.183 秒采用各自实际输出，不混用。日志/XML 为 `.newsroom/quality-foundation/chapter-7-final-full-tests.*`。

两个原失败用例在最终 JUnit 中均通过：真实四进程 latest index 更新约 2.287 秒、真实双 store planning 预算约 0.015 秒。新增 15 个确定性并发/时间回归也进入最终全量；原并发断言、真实时钟倒退和 deadline 拒绝规则保留。跳过类别与首轮相同，没有新增 skip。

机器可读结果保存为 `verification-results-20261010.json`，包含命令、实际统计、原失败用例最终结果、JUnit checksum、评测报告与源码身份。JUnit 原文件和完整日志保留在本地生成目录；该结果摘要不代替固定评测的逐例原始 JSON。

## 最终 smoke 结果

提交前必需 smoke 已完整通过，覆盖 compile、Harness/Research/API/service/recorded transport/architecture pytest，以及真实本地存储上的离线 AgentLoop 和 sources validation。pytest 没有失败或跳过；23 deselected 为既有线上 Research 标记规则。

离线 AgentLoop run 为 `test-agent-loop-ea23ced09b904541872fb94ee2b78349`，`status=succeeded`、`network_calls=0`、3 次本地 fixture LLM 调用、1 次真实本地 memory recall、一次 judge retry 后 accept。artifact 位于 `F:/github/NewsRoom/.newsroom/smoke/test-agent-loop-ea23ced09b904541872fb94ee2b78349/manifest.json`，manifest hash 为 `sha256:213061ed3a4ca81f2f806fbe0f0b008b1594dc2e5ef53f07bb31ea02fdd17d27`，activity receipt 为 `sha256:8d1da21bec28855761c4967e0e7cf4c489ad5aa5da6a2e426116d169724d3b32`。该 CLI smoke 的模拟候选只在测试服务中，生产实现不增加虚构业务结果。

`source validation` 为 `is_valid=true`、`error_count=0`、`warning_count=0`。最终全量也已独立通过，两套通过数分别记录，没有相加为全量数量。

## 全量失败的根因修复与复验

首轮两个并发失败均做了生产根因修复，原有真实并发测试保留：

- Windows run store 和共享 artifact 锁移除抢锁前初始化写入，直接对空文件 byte 0 加锁；保留原有有界竞争、错误分类、descriptor 生命周期、路径及文件身份安全校验。独立真实 Windows OS 探针确认空文件可以锁定/解锁而保持大小为零。新增同步事件控制的交错回归验证持锁竞争不会导致初始化写入失败，失败时仍保留 cause 并关闭 descriptor。
- filesystem、in-memory 和 JSONL 三类 planning observation store 均在 admission 锁内、历史 intent/receipt 读取后构造意图。确定性测试强制先启动的调用后入锁，并核对 1/2 槽预算、按提交顺序的时间和共享 deadline；历史读取期间 deadline 耗尽或真实时钟回退仍拒绝。没有改变 `admit_planning_call` 的预算、时钟或 deadline 校验。
- 锁/run store 受影响复验 **121 通过、2 项既有 Windows symlink 跳过，11.94 秒**；日志/XML 为 `.newsroom/quality-foundation/concurrency-lock-recheck.*`。
- planning reservation 受影响复验 **60 通过，3.36 秒**；日志/XML 为 `.newsroom/quality-foundation/planning-reservation-recheck.*`。
- 两处修复和全部新增回归代码写完后才运行集中检查。复验通过后重新运行最终全量；首轮原始失败日志和 XML 保留，最终结果不以这 181 项复验代替。
- 最终全量与 smoke 均在修复代码冻结后执行。核对测试 fixture 使用临时路径后，两者并行运行；smoke 使用独立 pytest basetemp、cache 和 JUnit 输出，其 CLI 产物在既有 `.newsroom/smoke/`，不与全量 fixture 共用状态。并行安排没有改变测试选集、预算或断言。
- 固定报告加入三份实际修复源码，核心来源从 55 增至 58 个文件，并重新生成。54 案例、语料、配置版本和冻结阈值未改变，最终报告仍通过且不具备推广收益。

## 首轮全量失败与跳过诊断

首轮完整执行全部默认选集并收集到两项失败，不能作为第 7 节通过证据：

1. `test_concurrent_processes_serialize_latest_index_updates`：四进程真实并发保存时，一个 child 在 `_exclusive_file_lock` 报 `LOCK_UNAVAILABLE`。失败耗时约 1.715 秒，小于 30 秒加锁上限；诊断指向锁文件零字节初始化与其他进程已持锁之间的竞争，不能通过扩大 timeout 或跳过 Windows 来解决。
2. `test_parallel_store_instances_share_one_planning_budget`：约 0.017 秒内 `admit_planning_call` 报 `planning_turn_timeout`。调用意图在取得序列化锁之前记录 admitted time，线程实际提交顺序反转后被识别为时钟倒退；应在原子准入边界采样，保留真实时钟倒退、冻结预算和 deadline 校验。

115 项跳过按 JUnit 的原始原因归类为：73 项 PostgreSQL opt-in、10 项 Docker opt-in、3 项 Redis opt-in，以及 29 项当前 Windows symlink 不可用。24 项 deselected 是仓库既有线上 Research 标记排除。没有新增 skip 或改变默认选集。首轮有 561 条既有依赖/服务探测警告，最终结果按复验重新记录。

## 固定评测身份与收益结论

最终报告 JSON 内容保存于 `fixed-quality-report-20261010.json`，仅将换行归一化为 LF 便于版本控制；已核对与 CLI 原报告的全部 JSON 内容相同，没有手工重写结果。以下报告 checksum 对应提交中的 LF 文件字节。

| 身份 | 实际值 |
| --- | --- |
| 评测时 Parent HEAD | `3f86249c355aab369f2e8590dc9772b1618fde3b` |
| 核心源码文件数 | `58` |
| 核心源码聚合 SHA-256 | `60f163424056c14e2b27eb0024f7dac51390cf9027fde0579b3307f402b03a21` |
| 固定案例 SHA-256 | `a6f3c68fc4942fdd5f971cb74e374b31b554b99e55ba7fda3e0e5bacd2b0897c` |
| 配置 SHA-256 | `6b7538301d37a41ed6f4b034c6394a44e830a10eb931417dfffb8b39e64879e1` |
| 语料输入 SHA-256 | `876b807e14de19bc4cda146351e75476efae55394a62128fe4e229ed8e4885f2` |
| 报告 SHA-256 | `d528c8f3016c5df1bb04ec327e0aeaad688bf5fe724d5efa424b8662415e4853` |

评测在工作树代码上运行，Parent HEAD 不能代替本轮实现版本。逐文件 checksum 标识实际受测工作树字节，已逐一与当前 58 个核心文件核对且没有差异。源码 checksum 按该 Windows 工作树的原始字节计算，跨平台 checkout 的换行转换可能改变字节 checksum，重放时应重新生成报告；`worktree_dirty=null` 表示评测器没有自行读取 Git 状态，不代表工作树干净；本轮代码和证据将在检查通过后共同提交。版本、阈值、实际启用的评测配置和逐例输入/expected/actual/reason 均在原报告中。

固定 54 例的 case pass、input integrity、claim qualification、constraint preservation、fallback accuracy、control-field rejection 均为 1.0。成对 Hit Rate、evidence coverage、required type coverage、source locator coverage、citation accuracy 和 MRR 的 baseline/candidate 均为 1.0，delta 均为 0.0。报告 `passed=true`，但 `rollout_eligible=false`，原因 `hit_rate_gain_below_threshold`：没有达到冻结的 0.01 绝对 Hit Rate 增量。质量通过不构成候选试验扩大启用的资格。

## 发布、回滚与条件待办

`research_input_quality_enabled=False` 和 `RetrievalCandidateExperimentConfig(enabled=False)` 均保持默认关闭。输入/Claim 功能仅适用于显式启用、已配置动态 runner 的单篇论文路径；检索候选没有获得扩大启用资格。本轮没有执行部署或修改外部服务配置。

新请求关闭输入质量开关后使用既有默认路径；在途 run 必须保留原来的精确 Graph/gate 和输入身份，不静默切换。候选失败使用原始计划，关闭配置后新会话不调用候选 worker。代码回滚应部署已验证版本，并保留原 transcript、gate decision、来源修订及 artifact；精确身份不匹配时拒绝恢复。详细开关、V1 条件、冻结阈值和回滚步骤见 `release-controls-20261008.md`。

`context-trigger-audit-20261008.md` 核查的 119 份真实本地持久化记录未提供合格的窗口证据丢失、条件漂移或显著重复读取成本触发。任务 6.6–6.12 仍是未触发的条件待办；测试夹具不能充当生产触发证据，不实现提前启用的连续性平台。当前范围为 90 项无条件交付和 7 项条件任务，不能声称 97 项全部实现。

## 提交前状态

全量、smoke、compile、固定评测均通过。`tasks.md` 本轮 90 项无条件任务已验收，7 项条件任务保留；`prd.md` 更新为 P0/P1 与观察能力已实现、P2 未触发。证据目录受仓库 `openspec/` ignore 规则影响，本次显式加入版本控制；`.newsroom/` 生成日志保留本地，不作为新的运行库。最终严格校验与 staged diff 检查通过后，提交本轮代码、测试及文档。

提交范围检查发现两个新增文件末尾多余空行，仅移除这些空行，未改变语句或断言。清理后 gate/固定案例/CI 三个受影响文件复验通过，compile 与固定 CLI 再次通过，保存报告重新生成并核对最终源码字节。该格式清理发生在完整全量/smoke 之后，完整测试结果对应相同逻辑实现；受影响补充结果为 `.newsroom/quality-foundation/final-eof-normalization-check.*`。
