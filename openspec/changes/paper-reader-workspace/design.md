## Context

用户明确要求“章节精读”读取转换后的论文全文，不接受把内嵌 PDF 或摘要作为主要阅读方式。当前 Research reader 依赖分析完成，检索文档还可能过滤参考文献及附录，不能直接作为全文回退。

## Goals / Non-Goals

**Goals:** 真实全文、源顺序、原生文字和图表、独立完整性门禁、桌面阅读连续性。

**Non-Goals:** 不接入新的 OCR 或模型服务；不在本次增加任意 PDF/压缩包上传转换；不伪造 AI 回复、图像校验、PDF 页码或 bbox；不新增移动端界面。

## Decisions

1. 默认三栏章节精读：约 190px 目录、弹性白色正文、330px 辅助区。浅紫背景、玻璃辅助面板与主页一致。提供专注及字号调整；原正式阅读路由保留。
2. 服务端从已公开 Paper 的 arXiv URL 确定源，用 parse5 将 LaTeXML article 转为 typed blocks。标题身份、正文长度、目录、段落覆盖、图片覆盖及唯一 ID 必须通过；脚本等不能进入文章。结构化正文按源顺序保留 bibliography/appendix。检索或分析 sections 不作全文 fallback。
3. 资源从源文档枚举：只访问同一 arXiv paper/version 的 HTTPS 路径，禁止重定向、外部地址及路径逃逸。有请求期限、并发和字节上限；验证 PNG/SVG 尺寸，计算真实 SHA-256。SVG 拒绝活动内容或外部引用，并通过带 sandbox CSP 的图片响应交付。全部需要的图片验证后才将 document 标为 compiled。
4. HTML 源使用真实 sourceLocator 定位，不虚构 PDF page/bbox。PaperVisualAsset.pageNumber 对这种来源可省略。多面板属于同一个 figure block；本地资源 URL 带 checksum。表格为结构化 cells，公式由 KaTeX 渲染，源算法文字仍作为同一图块保留。
5. 转换与问答独立。当前 arXiv 转换不表示 Research AI 已就绪，因此辅助区保留草稿、禁用发送。已有真实 ask BFF 使用当前 Research endpoint，失败或取消保留问题；正式回答和证据只能来自真实接口。正文不得包含 AI 改写。
6. Notebook 按 paper id 存在当前浏览器，可导出 Markdown；恢复问题、字号、章节和 PDF 对照页码，失败保留内存内容并提示。用户可选论文概览/PDF 对照，刷新后仍默认章节精读。
7. 设计版阅读入口使用安全 returnTo，回到原列表保留筛选。前端 build 不与 dev 并行写入 .next。

## Risks / Trade-offs

- 首版转换依赖 arXiv 已提供 LaTeXML 全文；没有该源或资源/完整性门禁失败时显示全文未就绪，不声称通用 PDF/OCR 转换已经完成。
- 首次访问需取得并验证全文及资源；采用有界共享内存缓存和 in-flight 去重，无自动无限重试。缓存不是长期存储。
- 原文 HTML 的转换质量受上游 LaTeXML 影响；真实论文验证覆盖正文到附录末尾和所有图片，但不等价于所有论文格式均已支持。
- AI 与云端笔记需要独立配置，不把可阅读正文当作分析已完成。
- 其他任务共享工作树；只提交阅读器相关路径，隔离 smoke 与当前前端检查分别记录证据。

## Migration Plan

设计入口切入新工作区，保留正式阅读路由。无持久化数据迁移；撤回设计入口可停止使用新工作区。
