import { describe, expect, it } from "vitest"
import { normalizePaperQuery, queryPapers } from "@/lib/papers/query"
import type { Paper } from "@/lib/papers/types"

describe("paper catalogue query", () => {
  it("keeps the survey condition when opening more guided results", () => {
    const items = [paper({ id: "survey", title: "Agent Survey" }), paper({ id: "review", title: "Agent Review" }), paper({ id: "other", title: "Agent Experiments" })]
    expect(queryPapers(items, { q: "agent", paperType: "survey" }).papers.map(item => item.id).sort()).toEqual(["review", "survey"])
  })
  it("intersects lexical terms, topic, feature, and closed date filters", () => {
    const matching = paper({
      id: "matching",
      title: "Agent Systems",
      abstractSnippet: "Planning with verified tools.",
      publishedAt: "2026-05-15T16:30:00Z",
      tags: ["cs.AI"],
      repoUrl: "https://github.com/example/agent-system"
    })
    const wrongTopic = paper({ id: "wrong-topic", tags: ["cs.LG"] })
    const outsideDate = paper({ id: "outside-date", publishedAt: "2026-05-17T00:00:00Z" })
    const noCode = paper({ id: "no-code", repoUrl: undefined })

    const result = queryPapers(
      [matching, wrongTopic, outsideDate, noCode],
      {
        q: "agent planning",
        topic: "CS.ai",
        task: "agents",
        method: "planning",
        has: "code",
        from: "2026-05-15",
        to: "2026-05-15"
      },
      "2026-05-20T00:00:00Z"
    )

    expect(result.papers.map((item) => item.id)).toEqual(["matching"])
    expect(result.query).toMatchObject({
      q: "agent planning",
      terms: ["agent", "planning"],
      topic: "CS.ai",
      task: "agents",
      method: "planning",
      from: "2026-05-15",
      to: "2026-05-15",
      has: ["code"],
      sort: "relevance"
    })
  })

  it("orders lexical matches by deterministic field relevance", () => {
    const abstractMatch = paper({
      id: "abstract-match",
      title: "General Systems",
      abstractSnippet: "A practical agent planning framework.",
      githubStars: 10_000
    })
    const titleMatch = paper({
      id: "title-match",
      title: "Agent Planning",
      abstractSnippet: "A practical framework.",
      githubStars: 1
    })

    const result = queryPapers([abstractMatch, titleMatch], { q: "agent planning", sort: "relevance" })

    expect(result.papers.map((item) => item.id)).toEqual(["title-match", "abstract-match"])
  })

  it("falls back from relevance to trending when no query is present", () => {
    const result = queryPapers([
      paper({ id: "low", githubStars: 1 }),
      paper({ id: "high", githubStars: 50 })
    ], { sort: "relevance" })

    expect(result.query.sort).toBe("trending")
    expect(result.papers.map((item) => item.id)).toEqual(["high", "low"])
  })

  it("ignores invalid date inputs and excludes unpublished papers from results and corpus range", () => {
    const result = queryPapers([
      paper({ id: "public-old", publishedAt: "2026-04-01T00:00:00Z" }),
      paper({ id: "public-new", publishedAt: "2026-05-20T00:00:00Z" }),
      paper({ id: "private-newer", publishedAt: "2026-06-01T00:00:00Z", isPublished: false })
    ], { from: "not-a-date", to: "2026-02-31" })

    expect(result.papers.map((item) => item.id)).toEqual(["public-old", "public-new"])
    expect(result.query.from).toBeUndefined()
    expect(result.query.to).toBeUndefined()
    expect(result.earliestPublishedAt).toBe("2026-04-01T00:00:00Z")
    expect(result.latestPublishedAt).toBe("2026-05-20T00:00:00Z")
  })

  it("keeps period semantics deterministic when a clock is supplied", () => {
    const result = queryPapers([
      paper({ id: "inside", publishedAt: "2026-05-13T00:00:00Z" }),
      paper({ id: "boundary", publishedAt: "2026-05-12T00:00:00Z" }),
      paper({ id: "outside", publishedAt: "2026-05-11T23:59:59Z" })
    ], { period: "weekly", sort: "newest" }, "2026-05-19T00:00:00Z")

    expect(result.papers.map((item) => item.id)).toEqual(["inside", "boundary"])
  })

  it("normalizes repeated search whitespace and known feature filters", () => {
    expect(normalizePaperQuery({ q: "  Agent   Agent planning ", has: "citation,unknown,pdf" })).toMatchObject({
      q: "Agent Agent planning",
      terms: ["agent", "planning"],
      has: ["pdf", "citation"]
    })
  })
})

function paper(overrides: Partial<Paper> = {}): Paper {
  return {
    id: "paper",
    slug: overrides.id ?? "paper",
    title: "Agent Systems",
    abstractSnippet: "Planning with verified tools.",
    authors: ["Alice Example"],
    publishedAt: "2026-05-15T00:00:00Z",
    tags: ["cs.AI"],
    taskRefs: [{ id: "task-agents", slug: "agents", name: "Agents" }],
    methodRefs: [{ id: "method-planning", slug: "planning", name: "Planning" }],
    repoUrl: "https://github.com/example/paper",
    isPublished: true,
    ...overrides
  }
}
