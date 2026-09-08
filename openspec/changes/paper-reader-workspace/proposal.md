## Why

用户需要在自己的阅读器里阅读转换后的整篇论文，而不是摘要或内嵌 PDF。设计版论文页需要一个延续 Agora 主页视觉的桌面工作区，验证正文、图表、公式、参考文献与附录的完整阅读体验。

## What Changes

- 新增 `/design-demo/papers/[slug]/read`，默认“章节精读”，提供目录、原文、辅助提问和本地笔记。
- 先支持公开 arXiv LaTeXML 全文源：将由论文源码生成的结构化 HTML 转换成自己的 `PaperDocument` blocks，不嵌入上游网页。
- 按源顺序保留章节、段落、公式、表格、多面板图片、参考文献和附录；正文完整性与真实资源校验通过后才发布。
- 解析不依赖 AI 分析结果；不会用摘要、检索片段或 AI 文案代替正文。
- PDF 保留为可选“PDF 对照”；章节全文不可用时给出状态与重试，不自动切换为 PDF。
- 保留设计列表筛选及返回路径，提供阅读位置、字号、问题草稿和笔记恢复。

## Capabilities

### New Capabilities
- `paper-reader-workspace`: Agora 桌面论文全文阅读工作区。

### Modified Capabilities

无。

## Impact

仅涉及 frontend 的设计路由、BFF、source 编译服务和 Open Reader 的呈现适配。新增 parse5 runtime 依赖。无数据库迁移，不恢复已退休的 legacy API。直接上传任意 PDF/LaTeX 压缩包、OCR 服务和 AI 分析后端的独立解析编排不在本次可体验版本范围内。
