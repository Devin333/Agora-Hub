import { parse, type DefaultTreeAdapterMap } from "parse5"
import type { Paper } from "@/lib/papers/types"
import type { PaperBlock, PaperDocumentResponse, PaperInlineSpan, PaperVisualAsset } from "@/lib/paper-reader/types"

type Node = DefaultTreeAdapterMap["node"]
const children = (node: Node): Node[] => "childNodes" in node ? node.childNodes : []
const tag = (node: Node): string => "tagName" in node ? node.tagName : ""
const attr = (node: Node, name: string): string => "attrs" in node ? node.attrs.find(item => item.name === name)?.value || "" : ""
const has = (node: Node, name: string): boolean => attr(node, "class").split(/\s+/).includes(name)
const descendants = (node: Node, match: (node: Node) => boolean): Node[] => children(node).flatMap(child => [...(match(child) ? [child] : []), ...descendants(child, match)])
const plain = (node: Node): string => node.nodeName === "#text" ? (node as DefaultTreeAdapterMap["textNode"]).value : tag(node) === "math" ? attr(node, "alttext") : children(node).map(plain).join("")
const text = (node: Node): string => plain(node).replace(/\s+/g, " ").trim()
const SKIP = new Set(["script", "style", "nav", "header", "footer", "noscript"])

/** Reconstruct the source article into our typed blocks; never embed upstream HTML. */
export function compileArxivSourceHtml(paper: Paper, html: string, sourceUrl: string, sourceHash: string): PaperDocumentResponse | null {
  const tree = parse(html)
  const pending: { node: Node; depth: number }[] = [{ node: tree, depth: 0 }]
  let nodeCount = 0
  while (pending.length) {
    const item = pending.pop()!
    if (++nodeCount > 80000 || item.depth > 128) return null
    for (const child of children(item.node)) pending.push({ node: child, depth: item.depth + 1 })
  }
  const article = descendants(tree, node => tag(node) === "article")[0]
  if (!article) return null
  const blocks: PaperBlock[] = []
  const assets: PaperVisualAsset[] = []
  const outline: NonNullable<PaperDocumentResponse["document"]>["outline"] = []
  const sourceTitle = descendants(article, node => has(node, "ltx_title_document"))[0]
  if (!sourceTitle || normalizeTitle(text(sourceTitle)) !== normalizeTitle(paper.title)) return null
  let sectionId = "source-abstract"
  let sectionLevel = 1
  let sequence = 0
  const coveredParagraphs = new Set<Node>()
  const coveredImages = new Set<Node>()
  let coreParagraphChars = 0
  const nodeIds = new WeakMap<Node, string>()
  const id = (node: Node) => {
    const existing = nodeIds.get(node)
    if (existing) return existing
    const value = attr(node, "id") || `source-block-${++sequence}`
    nodeIds.set(node, value)
    return value
  }
  const source = (node: Node) => `${sourceUrl}#${encodeURIComponent(attr(node, "id") || sectionId)}`
  function add(node: Node, body: Omit<PaperBlock, "id" | "paperId" | "sectionId">) {
    if (body.type !== "heading") {
      if (tag(node) === "p") coveredParagraphs.add(node)
      for (const paragraph of descendants(node, child => tag(child) === "p")) coveredParagraphs.add(paragraph)
    }
    blocks.push({ id: id(node), paperId: paper.id, sectionId, ...body, metadata: { sourceLocator: source(node), ...body.metadata } })
  }
  function heading(node: Node, value: string, level: number) {
    if (!attr(node, "id") && "parentNode" in node && node.parentNode && attr(node.parentNode, "id")) nodeIds.set(node, attr(node.parentNode, "id"))
    sectionId = id(node)
    sectionLevel = Math.min(6, Math.max(1, level))
    add(node, { type: "heading", text: value, level: sectionLevel })
    outline.push({ id: sectionId, blockId: sectionId, title: value, level: sectionLevel })
  }
  function paragraph(node: Node) {
    const content = inlineContent(node)
    let ancestor: Node | null = node
    let supplemental = false
    while (ancestor) {
      if (["ltx_abstract", "ltx_bibliography", "ltx_appendix"].some(name => has(ancestor!, name))) supplemental = true
      ancestor = "parentNode" in ancestor ? ancestor.parentNode : null
    }
    if (!supplemental) coreParagraphChars += content.text.length
    if (content.text.trim()) add(node, { type: "paragraph", text: content.text, metadata: { inlineSpans: content.spans } })
  }
  function walk(node: Node): void {
    const name = tag(node)
    if (SKIP.has(name) || has(node, "ltx_authors") || has(node, "ltx_title_document") || has(node, "ltx_rdf") || has(node, "ltx_pagination")) return
    if (name === "section") {
      const parentId = sectionId, parentLevel = sectionLevel
      for (const child of children(node)) walk(child)
      sectionId = parentId
      sectionLevel = parentLevel
      return
    }
    if (/^h[2-6]$/.test(name)) { heading(node, text(node), Number(name[1]) - 1); return }
    if (has(node, "ltx_abstract")) {
      heading(node, "Abstract", 1)
      for (const child of children(node)) if (!/^h\d$/.test(tag(child))) walk(child)
      return
    }
    if (name === "figure") {
      const captionNode = children(node).find(child => tag(child) === "figcaption")
      const caption = captionNode ? text(captionNode) : ""
      const labelNode = captionNode ? descendants(captionNode, child => has(child, "ltx_tag_figure") || has(child, "ltx_tag_table"))[0] : undefined
      const label = labelNode ? text(labelNode).replace(/[:.]\s*$/, "") : (has(node, "ltx_table") ? "Table" : "Figure")
      const tables = descendants(node, child => tag(child) === "table")
      if (has(node, "ltx_table") && tables.length) {
        add(node, { type: "table", caption, label, metadata: { tableModel: tableModel(tables[0]) } })
        return
      }
      const images = descendants(node, child => tag(child) === "img" || (tag(child) === "object" && attr(child, "type") === "image/svg+xml"))
      if (images.length) {
        const figureAssets: string[] = []
        for (const image of images) {
          coveredImages.add(image)
          const width = Number(attr(image, "width")), height = Number(attr(image, "height"))
          if (!Number.isFinite(width) || !Number.isFinite(height) || width < 1 || height < 1 || width > 20000 || height > 20000) throw new Error("invalid_figure_dimensions")
          const assetId = `source-${assets.length + 1}`
          assets.push({ assetId, paperId: paper.id, kind: "figure", fileName: assetId, mimeType: "", checksum: "", width, height, label, caption,
            metadata: { sourceProvider: "arxiv-html", sourceLocator: source(node), sourceUrl: new URL(attr(image, tag(image) === "object" ? "data" : "src"), sourceUrl).href, publicUrl: `/api/papers/${encodeURIComponent(paper.id)}/source-assets/${assetId}` },
          })
          figureAssets.push(assetId)
        }
        add(node, { type: "figure", label, caption, assetId: figureAssets[0], metadata: { assetIds: figureAssets } })
        return
      }
      const sourceText = children(node).filter(child => tag(child) !== "figcaption").map(child => inlineContent(child).text).filter(Boolean).join("\n")
      if (!sourceText) throw new Error("figure_content_missing")
      add(node, { type: "figure", label, caption, metadata: { sourceText } })
      return
    }
    if (name === "table" && (has(node, "ltx_equation") || has(node, "ltx_equationgroup"))) {
      const formulas = descendants(node, child => tag(child) === "math")
      for (const formula of formulas) add(formula, { type: "equation", text: mathLatex(formula), label: "" })
      return
    }
    if (name === "table") { add(node, { type: "table", label: "", metadata: { tableModel: tableModel(node) } }); return }
    if (name === "math") { add(node, { type: "equation", text: mathLatex(node), label: "" }); return }
    if (has(node, "ltx_bibitem") || name === "p" || name === "pre" || has(node, "ltx_listing")) { paragraph(node); return }
    if (name === "li") {
      if (!descendants(node, child => ["p", "li", "figure", "table"].includes(tag(child))).length) paragraph(node)
      else for (const child of children(node)) walk(child)
      return
    }
    if (has(node, "ltx_note")) { paragraph(node); return }
    for (const child of children(node)) walk(child)
  }
  walk(article)
  const paragraphChars = blocks.filter(block => block.type === "paragraph").reduce((count, block) => count + (block.text?.length || 0), 0)
  const sourceParagraphs = descendants(article, node => tag(node) === "p" && Boolean(text(node)))
  const retainedParagraphs = blocks.filter(block => block.type === "paragraph").length
  if (outline.length < 3 || coreParagraphChars < 3000 || paragraphChars < 3000 || sourceParagraphs.some(node => !coveredParagraphs.has(node))) return null
  if (descendants(article, node => tag(node) === "img" || tag(node) === "object").some(node => !coveredImages.has(node))) return null
  if (new Set(blocks.map(block => block.id)).size !== blocks.length) return null
  const compiledAt = new Date().toISOString()
  return {
    paper,
    document: { paperId: paper.id, schemaVersion: "arxiv-source-reader-v1", status: "compiling", title: paper.title, compiledAt, sourceHash, paper, outline, blocks,
      auxiliary: { sourceUrl, sourceFormat: "arxiv-latexml", sourceParagraphs: sourceParagraphs.length, retainedParagraphs } },
    manifest: { paperId: paper.id, schemaVersion: "arxiv-source-reader-v1", createdAt: compiledAt, sourceHash, provider: "arxiv-html", assets },
    status: { paperId: paper.id, status: "compiling", updatedAt: compiledAt, diagnostics: [], gateReport: { passed: false, sourceTitleMatched: true, sourceParagraphs: sourceParagraphs.length, retainedParagraphs } },
  }
}

function normalizeTitle(value: string) { return value.normalize("NFKC").replace(/[^\p{L}\p{N}]/gu, "").toLowerCase() }
function mathLatex(node: Node) { return attr(node, "alttext") || text(descendants(node, child => tag(child) === "annotation" && attr(child, "encoding") === "application/x-tex")[0] || node) }

function inlineContent(node: Node): { text: string; spans: PaperInlineSpan[] } {
  let value = ""
  const spans: PaperInlineSpan[] = []
  function append(child: Node) {
    if (SKIP.has(tag(child))) return
    if (tag(child) === "math") {
      const latex = mathLatex(child), start = value.length
      value += latex
      spans.push({ type: "math", text: latex, latex, start, end: value.length })
      return
    }
    if (tag(child) === "a" && attr(child, "href").includes("#")) {
      const label = text(child), start = value.length
      value += label
      spans.push({ type: "ref", text: label, targetBlockId: decodeURIComponent(attr(child, "href").split("#").pop() || ""), start, end: value.length })
      return
    }
    if (child.nodeName === "#text") value += (child as DefaultTreeAdapterMap["textNode"]).value.replace(/\s+/g, " ")
    else { if (tag(child) === "br") value += " "; for (const item of children(child)) append(item) }
  }
  append(node)
  const leading = value.length - value.trimStart().length
  return { text: value.trim(), spans: spans.map(span => ({ ...span, start: span.start - leading, end: span.end - leading })) }
}

function tableModel(table: Node) {
  return { rows: descendants(table, node => tag(node) === "tr").map(row => ({ cells: children(row).filter(node => ["td", "th"].includes(tag(node))).map(cell => ({
    text: text(cell), colspan: Math.min(100, Math.max(1, Number(attr(cell, "colspan")) || 1)), rowspan: Math.min(100, Math.max(1, Number(attr(cell, "rowspan")) || 1)), align: has(cell, "ltx_align_left") ? "left" : has(cell, "ltx_align_right") ? "right" : "center",
  })) })) }
}

/** SVG is served only as an image under a sandbox CSP; reject active or external content. */
export function sourceSvgDimensions(value: string): { width: number; height: number } | null {
  const tree = parse(value)
  const nodes: Node[] = [tree]
  let root: Node | undefined
  let count = 0
  const allowed = new Set(["html", "head", "body", "svg", "g", "defs", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan", "title", "desc", "use", "clipPath", "mask", "linearGradient", "radialGradient", "stop"])
  while (nodes.length) {
    const node = nodes.pop()!
    if (++count > 30000 || node.nodeName === "#documentType") return null
    const name = tag(node)
    if (name && !allowed.has(name)) return null
    if (name === "svg" && !root) root = node
    if ("attrs" in node) for (const attribute of node.attrs) {
      const key = attribute.name.toLowerCase(), item = attribute.value.trim()
      if (key.startsWith("on") || key === "style" || key === "src") return null
      if (key === "href" && !/^#[\w.-]+$/.test(item)) return null
      if (/url\s*\(/i.test(item) && !/^url\(#[\w.-]+\)$/.test(item)) return null
    }
    nodes.push(...children(node))
  }
  if (!root) return null
  const box = attr(root, "viewBox").trim().split(/[\s,]+/).map(Number)
  const width = box[2], height = box[3]
  return box.length === 4 && box.every(Number.isFinite) && width > 0 && height > 0 && width * height <= 40000000 ? { width, height } : null
}
