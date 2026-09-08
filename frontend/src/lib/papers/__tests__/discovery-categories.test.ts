import { describe, expect, it } from "vitest"
import { buildPaperCategories, previewCategories } from "../discovery-categories"
import type { Paper } from "../types"

const paper: Paper = { id: "p", slug: "p", title: "Paper", authors: [], abstractSnippet: "", publishedAt: "2026-01-01", tags: ["cs.AI", "cs.AI"], methodRefs: [{ id: "llm", slug: "method-large-language-models", name: "LLM" }], taskRefs: [{ id: "task", slug: "task-language-models", name: "Language Models" }], isPublished: true }

describe("paper discovery categories", () => {
  it("uses public unique paper associations and canonical aliases, never catalog statistics", () => {
    const result = buildPaperCategories([paper, paper, { ...paper, id: "draft", isPublished: false }], { methods: [{ slug: "large-language-model", name: "Language models", nameZh: "大语言模型" }, { slug: "unused", name: "Unused" }], tasks: [] })
    expect(result.topics).toEqual([{ value: "cs.AI", code: "cs.AI", name: "Artificial intelligence", nameZh: "人工智能", count: 1 }])
    expect(result.methods.map(({ value, count }) => ({ value, count }))).toEqual([{ value: "large-language-model", count: 1 }, { value: "unused", count: 0 }])
    expect(result.tasks[0]).toMatchObject({ value: "language-models", count: 1 })
  })

  it("retains categories past the old top-eight truncation", () => {
    expect(buildPaperCategories([{ ...paper, tags: Array.from({ length: 12 }, (_, i) => `tag-${i}`) }]).topics).toHaveLength(12)
  })

  it("shows five items while keeping a selected item outside the top five visible", () => {
    const items = Array.from({ length: 8 }, (_, i) => ({ value: `${i}` }))
    expect(previewCategories(items, "7", false).map(item => item.value)).toEqual(["0", "1", "2", "3", "7"])
    expect(previewCategories(items, "7", true)).toEqual(items)
    expect(previewCategories(items, "", false)).toEqual(items.slice(0, 5))
  })
})
