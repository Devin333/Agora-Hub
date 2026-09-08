import { describe, expect, it } from "vitest"
import { compileArxivSourceHtml, sourceSvgDimensions } from "../reader-source-compiler"
import { sourceHtml, sourcePaper } from "./source-fixture"

const compile = (html = sourceHtml()) => compileArxivSourceHtml(sourcePaper, html, "https://arxiv.org/html/2605.22343v1", "source-sha256")

describe("native full-text source compiler", () => {
  it("retains source order through references and appendices with math, tables and grouped image panels", () => {
    const result = compile(sourceHtml(true))!
    const blocks = result.document!.blocks
    expect(result.document!.outline.map(section => section.id)).toEqual(["abstract", "S1", "bib", "A1"])
    expect(blocks.filter(block => block.type === "heading").map(block => block.text)).toEqual(["Abstract", "Introduction", "References", "Appendix A"])
    expect(blocks.find(block => block.id === "bib1")?.text).toBe("Author. Full reference entry.")
    expect(blocks.find(block => block.id === "appendix-p")?.text).toBe("Original appendix detail, not an AI summary.")
    expect(blocks.at(-1)?.text).toBe("Direct list detail.")
    expect(blocks.find(block => block.id === "p1")?.metadata?.inlineSpans).toEqual(expect.arrayContaining([expect.objectContaining({ type: "math", latex: "x^2" }), expect.objectContaining({ type: "ref", targetBlockId: "bib1" })]))
    expect(blocks.find(block => block.id === "E1")).toMatchObject({ type: "equation", text: "E=mc^2" })
    expect(blocks.find(block => block.id === "T1")?.metadata?.tableModel).toMatchObject({ rows: [{ cells: [{ text: "Method" }, { text: "Score" }] }, { cells: [{ text: "A" }, { text: "0.8" }] }] })
    expect(blocks.find(block => block.id === "F1")?.metadata?.assetIds).toEqual(["source-1", "source-2"])
    expect(result.manifest!.assets).toHaveLength(2)
    expect(result.manifest!.assets[0].pageNumber).toBeUndefined()
    expect(result.status).toMatchObject({ status: "compiling", gateReport: { passed: false } })
    expect(JSON.stringify(blocks)).not.toContain(sourcePaper.abstractSnippet)
  })
  it("never embeds upstream scripts or raw HTML", () => {
    const result = compile(sourceHtml().replace("Original abstract.", 'Original abstract.<script>unsafe()</script><span onclick="unsafe()">Text</span>'))!
    expect(JSON.stringify(result.document!.blocks)).not.toContain("unsafe")
    expect(JSON.stringify(result.document!.blocks)).not.toContain("onerror")
  })
  it("retains SVG source figures and rejects unretained images or active vectors", () => {
    const html = sourceHtml(true).replace('<img src="/html/2605.22343v1/x1.png" width="100" height="50">', '<object type="image/svg+xml" data="/html/2605.22343v1/fig.svg" width="100" height="50"></object>')
    expect(compile(html)?.manifest?.assets[0].metadata?.sourceUrl).toBe("https://arxiv.org/html/2605.22343v1/fig.svg")
    expect(compile(sourceHtml().replace("Original abstract.", '<img src="unhandled.png">'))).toBeNull()
    expect(sourceSvgDimensions('<svg viewBox="0 0 100 50"><defs><path id="p" d="M0 0"/></defs><use href="#p"/></svg>')).toEqual({ width: 100, height: 50 })
    expect(sourceSvgDimensions('<svg viewBox="0 0 100 50"><script>unsafe()</script></svg>')).toBeNull()
    expect(sourceSvgDimensions('<svg viewBox="0 0 100 50"><use href="https://evil.test/x"/></svg>')).toBeNull()
  })
  it("rejects mismatched, incomplete, duplicate or excessively nested sources", () => {
    expect(compile(sourceHtml().replace("Source reconstruction", "Different paper"))).toBeNull()
    expect(compile("<article><h1 class=ltx_title_document>Source reconstruction</h1><p>Abstract only</p></article>")).toBeNull()
    expect(compile(sourceHtml().replace('id="appendix-p"', 'id="p1"'))).toBeNull()
    expect(compile(sourceHtml().replace("Original abstract.", "<header><p>Unretained source</p></header>"))).toBeNull()
    expect(compile("<div>".repeat(150) + sourceHtml() + "</div>".repeat(150))).toBeNull()
  })
})
