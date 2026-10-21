## ADDED Requirements

### Requirement: PDF document structure has versioned source identity

PDF 解析结果 SHALL 具有由 Research domain 拥有的版本化 page/block 结构，包含 schema、normalization、parser/profile 指纹、source hash、稳定 block identity 及原始 artifact 引用，并通过现有 document/artifact persistence 边界持久化。结构 SHALL 与同一 ResearchDocument 的实体相互引用，不得形成 benchmark-only 的第二份业务真值。

#### Scenario: Persist and load a structured PDF document
- **WHEN** 一个 PDF 被正式 parser profile 解析并持久化后重新加载
- **THEN** 加载结果 SHALL 保持结构版本、source hash、block identity、element references 和内容 checksum 一致
- **AND** 原始 backend artifacts SHALL 可追溯，但不得无条件复制到每个下游 chunk

#### Scenario: Artifact profile does not match requested parse
- **WHEN** 已缓存解析结果的 source、schema、normalizer 或 parser/profile 指纹与请求不一致
- **THEN** 系统 SHALL 拒绝将该缓存标记为当前 profile 的有效解析产物，并按明确策略重解析或返回版本不兼容原因

### Requirement: Page and geometry conversion is explicit

PDF page/block 结构 SHALL 区分一基物理 PDF 页码、可选印刷页标签与原始 backend page identity；bbox SHALL 声明坐标空间、单位及适用变换。backend schema adapter SHALL 只转换一次页码和坐标，未知或越界值不得通过默认页、边界钳制或猜测坐标伪装成有效位置。

#### Scenario: Backend uses a zero based page index
- **WHEN** 获支持 backend schema 的 block 指定 `page_idx=0` 且输入确有第一页
- **THEN** adapter SHALL 将其转换为物理 PDF 第 1 页，并保留可追踪的原始字段/转换身份

#### Scenario: Page or bounding box is invalid
- **WHEN** backend 页码超出输入页数，或 bbox 坐标的单位/变换不能可靠确定
- **THEN** 输出 SHALL 保留可诊断的 invalid/unknown 状态与原始值，不能把该 block 静默移动到第一页、末页或整页框

### Requirement: Mixed content order preserves node ownership and coverage

规范结构 SHALL 保留 backend order、实际采用的 reading order、顺序状态/原因及父子节点身份；同一内容节点在其正文投影中 SHALL 只有一个明确 owner。不同位置的相同文字 SHALL 不因字符串相同被全局去重；caption SHALL 作为关联 caption 或可寻址 block 保留。

#### Scenario: Parent contains text already represented by children
- **WHEN** backend 同时输出一个容器汇总文本和具有稳定身份的内容 children
- **THEN** 正文序列化 SHALL 按节点所有权只输出一次内容，并能追溯选择依据

#### Scenario: Separate cells or paragraphs contain identical text
- **WHEN** 两个不同 source positions 的 cell 或 paragraph 具有相同字符串
- **THEN** 系统 SHALL 保留各自独立身份、位置和内容，不以字符串去重删除其中一个

### Requirement: Reading order repair is constrained by layout evidence

顺序修复 SHALL 使用已版本化适用策略，保持原始序列并记录修改原因。布局证据充分时 SHALL 处理栏内顺序、纵向分区内的栏序以及跨栏块边界；证据不足、坐标缺失或布局冲突时 SHALL 显式标记不确定，不无条件采用全页 `(y,x)`、固定左右二分或“所有跨栏块先读”的重排。

#### Scenario: A two column page includes a middle full width table
- **WHEN** 一个获支持页面上半部为双栏，中间有跨栏表格，下半部恢复双栏
- **THEN** reading order SHALL 在相应纵向分区内保留正文栏序，将表格放在上下分区之间，并记录原始和采用的序列

#### Scenario: Geometry is missing or conflicting
- **WHEN** 可靠几何信息不足以恢复栏关系或候选顺序违反父子/锚点约束
- **THEN** 系统 SHALL 保留可追溯原顺序并产生 uncertainty/degradation 原因，而不是声称顺序已被正确恢复

### Requirement: Cross page sections retain precise source coverage

section 及其下游投影 SHALL 根据组成 blocks 记录真实 `page_start/page_end` 和局部 source locators。跨页聚合 SHALL 不将全部内容仅定位到首个 page；人工 benchmark batch SHALL 不跨来源文档边界合成同一语义 section。

#### Scenario: A section continues onto the following PDF page
- **WHEN** 同一 section 的正文从物理第 2 页延续到第 3 页
- **THEN** section SHALL 表示覆盖 2–3 页，且其局部内容或 chunks SHALL 能追溯到实际页上的原始 blocks

#### Scenario: A benchmark batch contains unrelated documents
- **WHEN** 多个来源文档的页面仅为批处理目的被拼在同一输入 PDF
- **THEN** 页映射 SHALL 保留文档边界，规范化投影不得把边界两侧的正文合并为同一文档 section

### Requirement: Inline and display formula content survives normalization

公式处理 SHALL 区分 inline/display，保留 raw/normalized LaTeX 或合法公式内容、源位置、上下文锚点和可用的编号。HTML/text/Markdown 转换 SHALL 保护公式边界及内容，不把纯 LaTeX 的比较符当作 HTML 标签剥除，不凭上下文补写缺失数学项。可渲染 SHALL 不等于内容正确。

#### Scenario: Text contains inline math and a following display matrix
- **WHEN** backend 输出正文中的 inline math 和单独的 matrix/align 公式
- **THEN** 规范文档 SHALL 保留两者类型、内容与邻接关系，实际文本/公式投影 SHALL 可逐项追溯回对应 source block

#### Scenario: Formula renders but disagrees with the source
- **WHEN** 一个候选公式可通过受限渲染但缺乏足够内容正确性的依据
- **THEN** 系统 SHALL 不仅凭渲染成功将其认定为语义正确或覆盖为已验证成功结果

### Requirement: Table grid preserves spans empty cells and cell content

复杂表格 SHALL 保存可追溯的有序 cell-grid、行列身份、rowspan/colspan、header roles/paths 及 cell text/math 内容。空白、被合并格覆盖、missing 和 unreadable SHALL 可区分。重复/多层表头、无表头及单行表格 SHALL 不被静默合并、移列或清空；数值符号、小数点、单位、脚注和公式 SHALL 不被“纠错”改写。

#### Scenario: A complex table includes blank cells and merged headers
- **WHEN** 输入表格含 colspan/rowspan、多层重复表头和中间空 cell
- **THEN** grid SHALL 保留其几何占位、表头身份、原始内容和 source mapping，空 cell 后的值不能向左移列

#### Scenario: A table has one row and no explicit header
- **WHEN** backend 检测到有效单行无表头表格
- **THEN** 规范表格 SHALL 保留该行、各 cell 内容与无表头属性，不能因缺少第二行而返回空表

### Requirement: Production projections declare and validate information loss

`ResearchTable.rows` 等扁平投影 SHALL 明确来源 grid 和 projection version，不得代替复杂表格的唯一真值。实际 chunker、parent/row-group payload 及存储 round-trip SHALL 保留所需公式/cell 内容、顺序映射和 locator，无法表示的内容 SHALL 显式可寻址或报告 loss，而不能从 raw backend 偷补后声称投影无损。视觉描述 SHALL 与表格事实内容分离。

#### Scenario: Table row group becomes searchable chunks
- **WHEN** 一个包含多层表头和 cell math 的 TableGrid 经生产 chunker 生成 parent 与 row-group chunks
- **THEN** 实际消费 payload SHALL 包含对应 header path、cell 内容及 source mapping，且保真检查能定位有意省略或丢失的内容

#### Scenario: Visual description conflicts with recognized cells
- **WHEN** 表格还存在独立视觉描述且它与 cell 数值不一致
- **THEN** 系统 SHALL 不以描述覆盖 cell 真值或把描述当作通过 cell fidelity gate 的证据
