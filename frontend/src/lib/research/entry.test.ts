import { describe, expect, it } from "vitest"
import { researchQuestionHref, resolveResearchIntent, safeResearchHref } from "./entry"

describe("research entry", () => {
  it.each([
    ["找一个适合研究 Agent 的开源项目", "projects"],
    ["看看最近活跃的研究基础设施项目", "projects"],
    ["找 Agent 相关论文", "papers"],
    ["了解近期 AI 社区讨论", "community"],
    ["比较 RAG 方法并写报告", "reports"],
    ["Write a report about open source agents", "reports"],
  ] as const)("routes %s to %s", (question, module) => expect(resolveResearchIntent(question)).toEqual([module]))

  it("respects manual choice and asks for ambiguous or unknown destinations", () => {
    expect(resolveResearchIntent("找 Agent 论文", "projects")).toEqual(["projects"])
    expect(resolveResearchIntent("论文和开源项目")).toEqual(["papers", "projects"])
    expect(resolveResearchIntent("Agent")).toHaveLength(4)
  })

  it.each(["papers", "projects", "community", "reports"] as const)("carries the original question safely to %s", module => {
    const question = "比较 RAG & Agent 的论文？"
    const href = researchQuestionHref(module, question)
    const url = new URL(href, "https://agora.invalid")
    expect(url.searchParams.get("question")).toBe(question)
    expect(url.searchParams.get("q")).toContain("RAG & Agent")
    expect(url.searchParams.get("entry")).toBe("home")
    expect(url.searchParams.has("compose")).toBe(module === "reports")
    expect(safeResearchHref(href)).toBe(href)
  })

  it.each(["https://evil.test/projects", "//evil.test/projects", "/\\evil.test", "/admin?question=x", "/projects/../../admin", "javascript:alert(1)"])("rejects an unsafe return location %s", href => {
    expect(safeResearchHref(href)).toBeNull()
  })
})
