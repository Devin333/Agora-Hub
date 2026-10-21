# Codex 交接包：PDF 论文解析质量优化

> 生成日期：2026-10-21（Asia/Shanghai）
> 用途：将当前 Codex 会话上下文交给新的 Codex 窗口继续工作。
> 安全说明：本文件不包含任何 API key、密码、token 或 `.env` 内容。

---

## 1. 新窗口首先要知道什么

当前用户正在优化 Agora-Hub 项目的论文 PDF 解析质量，重点是：

1. 公式恢复和公式内容保真；
2. 双栏/混合栏/跨栏内容的阅读顺序；
3. 复杂表格的单元格、合并单元格、多层表头和数值内容；
4. 解析结果进入 `ResearchDocument` 和下游 RAG chunks 后是否继续丢失信息；
5. 如何建立可信的 parser benchmark、基线和发布验收门槛。

用户已经要求先写 PRD 和 OpenSpec 规划，不是立即修改解析器代码。

当前阶段：**规划已完成，尚未开始实现优化代码。**

---

## 2. 项目与 Git 状态

- 项目仓库：`F:\github\Agora-Hub`
- 当前工作分支：`codex/reportbench-cross-source-eval`
- 当前主 worktree 实际 Git 根目录：`F:/github/NewsRoom`
- Agora-Hub 目录与 NewsRoom 存在 worktree/路径关系，操作前必须确认 `git rev-parse --show-toplevel` 和当前 worktree。
- 当前项目工作树本来就有很多未提交修改和未跟踪文件。
- 不要执行：`reset`、`stash`、全量 `git add`、全量提交、覆盖其他 change 的文件。
- 当前只新增了本变更规划文档，没有修改生产代码。
- 仓库 `.gitignore` 当前忽略整个 `openspec/` 目录，所以这些 OpenSpec 文件不会自动出现在普通 `git status` 中。
- 本次没有修改 `.gitignore`，也没有执行 Git commit。

### 继续工作前建议执行

```powershell
Set-Location F:\github\Agora-Hub
git branch --show-current
git rev-parse --show-toplevel
git status --short --untracked-files=all
openspec status --change research-pdf-parsing-quality-optimization
```

---

## 3. 已完成的规划文档

OpenSpec change 名称：

```text
research-pdf-parsing-quality-optimization
```

目录：

```text
F:\github\Agora-Hub\openspec\changes\research-pdf-parsing-quality-optimization\
```

文件：

- PRD：
  `F:\github\Agora-Hub\openspec\changes\research-pdf-parsing-quality-optimization\prd.md`
- Proposal：
  `F:\github\Agora-Hub\openspec\changes\research-pdf-parsing-quality-optimization\proposal.md`
- Design：
  `F:\github\Agora-Hub\openspec\changes\research-pdf-parsing-quality-optimization\design.md`
- Tasks：
  `F:\github\Agora-Hub\openspec\changes\research-pdf-parsing-quality-optimization\tasks.md`
- Specs：
  `F:\github\Agora-Hub\openspec\changes\research-pdf-parsing-quality-optimization\specs\`

当前规格目录：

```text
specs/research-document-parse-fidelity/spec.md
specs/research-document-parse-quality-control/spec.md
specs/research-document-parsing-evaluation/spec.md
specs/parser-cascade-bakeoff-acceptance/spec.md
```

OpenSpec 状态：

```text
Progress: 4/4 artifacts complete
proposal: done
design: done
specs: done
tasks: done
```

已通过：

```powershell
openspec validate research-pdf-parsing-quality-optimization --strict
```

任务清单中共有 45 项实施任务，目前全部保持未完成状态，这是正确的，因为还没有开始实现。

---

## 4. PRD 的核心结论

### 4.1 不能只看 OmniDocBench 的历史分数

当前 OmniDocBench 结果主要是：

```text
图片页面 → 人工拼成批次 PDF → Marker → 原始 Marker JSON → Markdown 导出 → 官方 OmniDocBench evaluator
```

它不是完整的业务生产路径：

```text
真实 PDF → production parser/composition → ResearchDocument → chunker → evidence/RAG
```

因此必须区分三个评测轨道：

1. `backend_raw`：原始 parser/backend 输出质量；
2. `production_document`：实际 `ResearchDocument` 归一化结果质量；
3. `downstream_projection`：真实 chunker、存储、下游 payload 是否丢失信息。

不能用 raw JSON 反向补齐 production document，也不能把 raw 轨成绩冒充生产产品质量。

### 4.2 215 页的身份

现有 215 页已经被查看和用于诊断，不能重新随机切一部分就称为 unbiased held-out。

它们只能作为：

```text
exposed dev/regression set
```

正式 held-out 必须按真实来源论文、版本族和近重复文档分组，在优化前冻结。

### 4.3 当前优化范围

包含：

- page/block 有序结构和来源定位；
- backend order 与 production reading order 分离；
- inline/display 公式保真；
- table grid、rowspan/colspan、空 cell、表头路径、cell formula；
- parser quality probe、degraded/quarantined；
- 有界恢复和 execution receipt；
- 可复现评测、失败分母、版本指纹、回滚。

不包含：

- 不重新引入 MinerU；
- 不重建 RAG evaluator；
- 不更换模型/API；
- 不让 LLM 决定 workflow routing、quality gate、promotion 或 active skill；
- 不凭空补公式、表格或正文；
- 不把 visual description 当作表格单元格真值；
- 不立即修改生产代码。

---

## 5. OmniDocBench 历史基线

### 5.1 数据和版本

- benchmark 目录：`F:\benchmarks\AgoraHub-OmniDocBench`
- 官方源码 checkout：`F:\benchmarks\OmniDocBench`
- 官方源码 commit：`f133a71e9e91c3621c7ce8994200a7b394a06eb3`
- 数据 revision：`aa1ee96d106dbe53d0ae59474d75c6e6d9b53fec`
- scope：`academic_literature`
- 页面数：215
- batch size：25
- 总执行时间：约 2415.48 秒
- 每个 batch 执行成功，但这只表示批次执行和文件输出成功，不代表语义解析全部正确。
- 历史 parser 标识：`AgoraHub MarkerPdfDocumentParser + newsroom-marker:latest`
- `latest` 是可变标签，历史 manifest 没有完整不可变镜像身份，因此 B0 只能视为历史诊断基线。

关键文件：

```text
F:\benchmarks\AgoraHub-OmniDocBench\results\run_manifest.json
F:\benchmarks\AgoraHub-OmniDocBench\results\official\pred_quick_match_metric_result.json
F:\benchmarks\AgoraHub-OmniDocBench\results\official\pred_quick_match_run_summary.json
F:\benchmarks\AgoraHub-OmniDocBench\scripts\run_project_parser.py
```

### 5.2 官方 page-level 指标

以下是 page mean，不能和 sample mean 混用：

| 维度 | 指标 | 历史结果 | 有效页数 | 方向 |
|---|---:|---:|---:|---|
| 正文 | `Edit_dist` | `0.13502387702863677` | 191 | 越低越好 |
| 展示公式 | `Edit_dist` | `0.42048749122515916` | 56 | 越低越好 |
| 展示公式 | `CDM` | `0.6369432296808887` | 56 | 越高越好 |
| 表格 | `Edit_dist` | `0.21565061512912048` | 97 | 越低越好 |
| 表格 | `TEDS` | `0.7337957380978444` | 97 | 越高越好 |
| 表格 | `TEDS_structure_only` | `0.8577733151693889` | 97 | 越高越好 |
| 阅读顺序 | `Edit_dist` | `0.274559386858204` | 212 | 越低越好 |

另外：

- formula CDM sample mean：`0.5786448911222779`，不是 page mean；
- table TEDS sample mean：`0.7521874387769479`，不是 page mean；
- table structure-only sample mean：`0.8651866277058989`；
- reading order 的 double-column slice：约 `0.2706315`；single-column slice：约 `0.3691606`，因此不能简单断言“所有阅读顺序问题都由双栏造成”；
- historical notebook composite `74.52383635833654` 不是产品端到端质量分，不可作为发布门槛。

### 5.3 指标解释注意事项

- `Edit_dist` 是归一化编辑距离，官方实现使用：
  `Levenshtein.distance(pred, gt) / max(len(pred), len(gt))`。
- `CDM` 是公式字形/字符检测匹配类指标，不等于数学语义等价率。
- `TEDS` 是表格结构和内容相似度，不等于“85% 单元格正确”。
- `TEDS_structure_only` 只能说明结构子指标，不能直接推出 cell text accuracy。
- 公式/表格记录数不等于标注对象数，必须报告 match、unmatched、missing、error 等状态。

---

## 6. 当前实现中已经确认的风险

### 6.1 Marker adapter

文件：

```text
F:\github\Agora-Hub\backend\research\document\marker_pdf_parser.py
```

已确认：

- `_flatten_blocks` 会展开 parent/children，并设置 `_marker_parent_kind`；需要避免父节点和 children 重复写入正文。
- `_components_from_blocks` 将 sections、equations、tables、figures 分开积累；当前没有明确的跨类型 page/block 主顺序。
- section flush 的当前逻辑可能将跨页 section 的 page range 记录得不准确，需用真实多页 fixture 验证。
- `_block_text` 和 `_block_equation_text` 都会经过 `_html_to_text` / `_normalize_text`，可能导致公式边界、LaTeX 比较符或 inline math 信息丢失。
- `TextInlineEquation`/`textinlinemath` 当前会被视为 text，需要单独保留 inline math span。
- `_TableHTMLParser` 当前主要保存 cell text，不保存 rowspan/colspan。
- `_matrix_to_rows` 会过滤空 cell，可能导致列移位；第一行会被转成 columns，复杂表头信息可能丢失。
- `_block_pdf_rect` 已有 bbox 到 PDF 坐标转换能力，不要另造一套几何转换。
- `_page_number` 同时处理 `page/page_number`、`page_idx/page_id/page_index` 和 Marker path，但必须显式验证 schema，不允许 benchmark 导出层盲目 clamp。
- `NEWSROOM_MARKER_DOCKER_ARGS` 是 Docker runtime 参数，不应被用来传识别模型参数；新增 parser 选项应走 typed settings/composition。

### 6.2 Research domain

文件：

```text
F:\github\Agora-Hub\backend\research\domain\document.py
```

当前主要模型：

- `ResearchDocument`
- `ResearchSection`
- `ResearchFigure`
- `ResearchTable`
- `ResearchEquation`
- `ResearchReference`

`ResearchDocument` 当前有 sections/figures/tables/equations，但没有明确的跨类型 page/block order IR。

`ResearchTable` 当前主要是：

```python
columns: list[str]
rows: list[dict[str, Any]]
```

这对普通表格方便，但不能作为复杂表格的唯一真值。

### 6.3 Chunker

文件：

```text
F:\github\Agora-Hub\backend\research\document\chunker.py
```

当前正文、公式、图、表分别生成 chunks，已经有：

- formula references；
- table parent/row-group chunks；
- source locator；
- chunk manifest。

优化时应复用现有 chunker 类型和 locator 逻辑，重点验证结构化解析进入 chunk 后是否丢失，不要重写整个 RAG 检索流程。

### 6.4 Cascade / quality probe

文件：

```text
F:\github\Agora-Hub\backend\research\document\cascade_parser.py
```

已存在：

- `DocumentQualityProbe`；
- `QualityProbeResult`；
- parser attempts；
- PyMuPDF selectable text admission；
- cascade backend 路径。

后续应扩展现有确定性质量检查，而不是新增平行 quality system。

---

## 7. 历史 benchmark runner 的方法学限制

文件：

```text
F:\benchmarks\AgoraHub-OmniDocBench\scripts\run_project_parser.py
```

关键行为：

- 从图片生成 raster PDF，没有 selectable text layer；
- 每 25 页拼成一个批次 PDF；
- 通过 `MarkerPdfDocumentParser(command_runner=marker_runner)` 注入 plain `subprocess.run`；
- bypass 了 production execution adapter / receipt / Harness；
- 从 `document.metadata["parser_content_ref"]` 读取 Marker raw blocks；
- 用项目私有 helper 将 raw blocks 转为 Markdown；
- 导出时存在 page fallback/clamp 逻辑。

结论：

```text
历史 B0 = raw backend diagnostic
不是 production parser + ResearchDocument + chunker 的完整质量证明
```

正式实现前要新增：

- 不可变版本和 manifest；
- raw backend 轨；
- production document 轨；
- downstream projection 保真检查；
- 失败/不可评/超时完整分母；
- 真实多页 PDF 和 document-level held-out。

---

## 8. OpenSpec 设计原则

### 8.1 数据结构

拟增加 versioned page/block IR，最低应支持：

- `schema_version`
- `normalization_version`
- `source_hash`
- `parser_fingerprint`
- `profile_fingerprint`
- page physical number / printed label
- bbox coordinate system / unit / transform
- `block_id`
- `parent_id`
- `kind`
- `source_order`
- `reading_order`
- `order_status`
- `order_reason`
- `element_ref`
- `source_locator`
- inline/display math spans
- table grid cells with row/column/span/header role/status

不应把大 raw JSON、图像复制到每个 chunk；通过 artifact ref 连接。

### 8.2 阅读顺序

推荐策略：

1. 先保留 backend order；
2. 只在 bbox/布局证据可靠时做几何候选；
3. 按纵向 band 识别栏和跨栏块；
4. band 内再按栏序、栏内位置排序；
5. 保留 parent/child/caption 约束；
6. 证据不足时保留原顺序并标记 uncertainty/degraded；
7. 不采用无条件全页 `(y,x)`、固定左右二分或“所有跨栏块先读”。

### 8.3 公式

先解决转换链的信息损失：

- 区分 inline/display；
- 保存 raw/normalized LaTeX；
- 保留公式编号、page、bbox、上下文 offset；
- 不把 LaTeX `<`/`>` 误当 HTML；
- renderer 成功不代表数学内容正确；
- 没有可靠证据时，不补公式、不改写公式；
- ROI recovery 只能有界、可审计、受 Harness 控制。

### 8.4 表格

复杂表格真值应是 grid，不是 `rows: dict`：

- 保存 row/col index；
- 保存 rowspan/colspan；
- 保存空 cell、covered cell、missing、unreadable 状态；
- 保存多层/重复表头和 header path；
- 保存 cell 内公式和 source locator；
- 数字、负号、小数点、单位不能被自动“纠错”；
- `visual_description` 只作为辅助描述，不覆盖 cell 内容。

### 8.5 Harness 与安全

- LLM 只能生成 candidate，不能决定 route、gate、promotion、memory write 或 active skill；
- 任何 retry/replan/recovery 都必须有 max budget；
- parser/recovery 走现有 Harness `PLAN → EXECUTE → VERIFY`；
- 超时终止不确定时记录 `indeterminate`，不能算成功；
- LaTeX 禁止 shell escape；
- HTML 过滤 script/event handler/外链执行；
- 不把文档内容当 tool 指令；
- 不用任意 Docker argv 传业务识别参数。

---

## 9. 推荐下一步：M0 基线对齐

不要直接改公式或双栏算法，先做以下工作：

1. 确认当前 worktree 和并行 OpenSpec 状态；
2. 审计 Marker parser、ResearchDocument、chunker 的真实输入输出；
3. 固定历史 benchmark 的数据、evaluator、parser、镜像和配置指纹；
4. 建立 215 页 exposed dev/regression manifest；
5. 为公式、顺序、表格各抽取坏例和正常对照；
6. 运行真正的 production document baseline B1；
7. 检查 B1 哪些指标可忠实导出，哪些不可测；
8. 在任何算法改动前冻结 acceptance profile；
9. 再开始 M1 结构 IR 和 serializer/chunker 保真实现。

推荐首批工作文件：

```text
backend/research/domain/document.py
backend/research/document/marker_pdf_parser.py
backend/research/document/cascade_parser.py
backend/research/document/chunker.py
backend/research/document/chunk_manifest.py
backend/research/rag/evaluation/paper_parser_bakeoff_report.py
backend/research/rag/evaluation/paper_parser_bakeoff_ingest.py
```

推荐测试文件：

```text
tests/backend/research/document/test_source_format_and_pdf.py
```

相关命令：

```powershell
python -m scripts.dev compile
python -m scripts.dev test
python -m scripts.dev smoke
openspec validate research-pdf-parsing-quality-optimization --strict
```

规划阶段目前只保证 OpenSpec strict validation 和文档一致性检查通过，不代表上面这些代码检查已经执行或通过。

---

## 10. 并行 OpenSpec 变更协调

需要注意但不要直接修改的相关 change：

- `remove-mineru-pdf-backend`
  - 正在移除 MinerU，不要重新引入 MinerU。
- `research-rag-evaluation-protocol`
  - 正在建设 RAG 评测协议；本变更复用其 evaluator 边界，不新建平行 RAG evaluator。
- `research-evidence-context-quality-foundation`
  - 负责 evidence/context 基础能力；不要把 PDF parser 逻辑塞进去。
- `parser-cascade-bakeoff-acceptance`
  - 现有 parser bake-off acceptance spec；本变更只扩展 document parsing acceptance，不替换已有 cascade acceptance。

---

## 11. 用户之前遇到的模型/API背景

之前讨论过 OpenAI、第三方 OpenAI-compatible API、Asta、DeepSeek、`gpt-6-luna`、`gpt-5.6-sol` 等模型和 API 连通性问题。

这些凭据和 API key **不应写进本文件，也不应复制到新窗口消息中**。如果后续要跑评测，应从当前项目明确的 `.env` / composition 配置读取，并先确认实际读取路径，不要把 key 硬编码到代码、脚本或文档。

本次 PDF parsing 优化规划不依赖某个特定 LLM；优先走确定性 parser、结构化转换和现有 Harness 边界。

---

## 12. 给新窗口的建议开场指令

可以把下面这段直接发给新窗口：

```text
请先阅读 F:\\github\\Agora-Hub\\codex-handoff-pdf-parsing-quality-optimization.md。

继续处理 OpenSpec change：research-pdf-parsing-quality-optimization。
当前只完成了 PRD、proposal、design、specs、tasks，尚未修改生产代码。
请先执行只读审计，确认当前 worktree、AGENTS.md、相关 OpenSpec 并行变更和实际 parser/chunker 调用链，然后从 M0 基线对齐开始，不要直接修改公式、双栏或表格算法。
不要重引 MinerU，不要新建平行 RAG evaluator，不要泄露或硬编码 API key，不要覆盖其他未提交修改。
所有面向用户回复使用中文；任何代码修改完成后按项目要求运行匹配检查并提交明确范围的变更。
``` 

---

## 13. 当前结论

当前最重要的结论不是“某个分数低”，而是：

> 现有 benchmark 主要测的是 Marker raw output，而用户真正需要的是从 PDF 到 `ResearchDocument`、chunks、evidence 的整条生产路径。

因此下一步必须先补生产基线和阶段性保真检查，再根据错误样本优化公式、阅读顺序和复杂表格。否则很可能出现 benchmark 分数提升，但项目实际研究报告质量没有提升的情况。
