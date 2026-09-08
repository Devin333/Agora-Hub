import type { Paper } from "@/lib/papers/types"

export const sourcePaper: Paper = { id: "source-paper", slug: "source-paper", title: "Source reconstruction", abstractSnippet: "Metadata abstract is not full text", authors: ["Author"], publishedAt: "2026-05-21", tags: [], taskRefs: [], methodRefs: [], arxivUrl: "https://arxiv.org/abs/2605.22343v1", isPublished: true }

export function sourceHtml(images = false) {
  return `<article><h1 class="ltx_title_document">Source reconstruction</h1>
    <div id="abstract" class="ltx_abstract"><p id="abstract-p">Original abstract.</p></div>
    <section id="S1"><h2>Introduction</h2><p id="p1">${"Source paragraph with verifiable evidence. ".repeat(100)}<math alttext="x^2"></math> <a href="#bib1">[1]</a></p>
    ${images ? '<figure id="F1"><img src="/html/2605.22343v1/x1.png" width="100" height="50"><img src="/html/2605.22343v1/x2.png" width="100" height="50"><figcaption><span class="ltx_tag_figure">Figure 1:</span> Two panels.</figcaption></figure>' : ""}
    <figure class="ltx_table" id="T1"><table><tr><th>Method</th><th>Score</th></tr><tr><td>A</td><td>0.8</td></tr></table><figcaption><span class="ltx_tag_table">Table 1:</span> Results.</figcaption></figure>
    <table class="ltx_equation"><tr><td><math id="E1" alttext="E=mc^2"></math></td></tr></table></section>
    <section id="bib" class="ltx_bibliography"><h2>References</h2><ul><li class="ltx_bibitem" id="bib1">Author. Full reference entry.</li></ul></section>
    <section id="A1" class="ltx_appendix"><h2>Appendix A</h2><p id="appendix-p">Original appendix detail, not an AI summary.</p><ul><li>Direct <strong>list detail</strong>.</li></ul></section></article>`
}
