import { beforeEach, describe, expect, it, vi } from "vitest"
import { NextRequest } from "next/server"
import { GET as getCommunityListRoute } from "@/app/api/community/route"
import { GET as getCommunitySignalRoute } from "@/app/api/community/signals/[id]/route"
import { GET as getCommunitySignalsRoute } from "@/app/api/community/signals/route"
import { GET as getCommunityTopicRoute } from "@/app/api/community/topics/[slug]/route"
import { getCommunityList, getCommunitySignal, getCommunitySignals, getCommunityTopic } from "@/lib/community/server-data"

vi.mock("@/lib/community/server-data", () => ({
  getCommunityList: vi.fn(),
  getCommunitySignal: vi.fn(),
  getCommunitySignals: vi.fn(),
  getCommunityTopic: vi.fn()
}))

describe("community BFF routes", () => {
  beforeEach(() => {
    vi.mocked(getCommunityList).mockReset()
    vi.mocked(getCommunitySignal).mockReset()
    vi.mocked(getCommunitySignals).mockReset()
    vi.mocked(getCommunityTopic).mockReset()
  })

  it("returns Community Pulse list data with parsed filters", async () => {
    vi.mocked(getCommunityList).mockResolvedValueOnce({
      topics: [],
      allTopics: [],
      page: { items: [], total: 0, page: 2, pageSize: 12, hasNext: false },
      metrics: { totalTopics: 0, activeSources: 0, positiveCount: 0, negativeCount: 0, mixedCount: 0 },
      options: { sources: [], sentiments: [], topics: [], tags: [] },
      dataState: "empty",
      source: "empty",
      notices: []
    })

    const response = await getCommunityListRoute(
      new NextRequest(
        "http://localhost/api/community?q=agent&source=hackernews&sentiment=mixed&sort=controversial&topic=agents&page=2&pageSize=12"
      )
    )
    const payload = await response.json()

    expect(response.status).toBe(200)
    expect(payload.success).toBe(true)
    expect(getCommunityList).toHaveBeenCalledWith({
      q: "agent",
      source: "hackernews",
      sentiment: "mixed",
      sort: "controversial",
      topic: "agents",
      page: 2,
      pageSize: 12
    })
  })

  it("returns community topic details", async () => {
    vi.mocked(getCommunityTopic).mockResolvedValueOnce({
      id: "topic-1",
      slug: "agent-memory",
      title: "Agent memory",
      summary: "Public topic summary.",
      sourceType: "hackernews",
      sentiment: "mixed",
      tags: [],
      sourceDistribution: [],
      topDiscussions: [],
      representativeComments: [],
      timeline: [],
      notices: []
    })

    const response = await getCommunityTopicRoute(new NextRequest("http://localhost/api/community/topics/agent-memory"), {
      params: Promise.resolve({ slug: "agent-memory" })
    })
    const payload = await response.json()

    expect(response.status).toBe(200)
    expect(payload.success).toBe(true)
    expect(payload.data.topic.title).toBe("Agent memory")
  })

  it("returns Community Pulse signals with parsed PRD and legacy filters", async () => {
    vi.mocked(getCommunitySignals).mockResolvedValueOnce({
      items: [],
      allItems: [],
      allFiltered: [],
      clusters: [],
      facets: { sources: [], topics: [], sentiments: [] },
      nextCursor: null,
      page: { items: [], total: 0, page: 2, pageSize: 12, hasNext: false, nextCursor: null },
      metrics: {
        totalSignals: 0,
        periodSignals: 0,
        activeSources: 0,
        hotSignals: 0,
        controversialSignals: 0,
        heatSummary: "No community signals are available from the current data source."
      },
      dataState: "empty",
      source: "empty",
      notices: []
    })

    const response = await getCommunitySignalsRoute(
      new NextRequest(
        "http://localhost/api/community/signals?q=agent&source=github_discussion&sentiment=mixed&sort=trending&period=weekly&topic=agents&page=2&pageSize=12"
      )
    )
    const payload = await response.json()

    expect(response.status).toBe(200)
    expect(payload.success).toBe(true)
    expect(getCommunitySignals).toHaveBeenCalledWith({
      q: "agent",
      source: "github",
      sentiment: "mixed",
      sort: "hot",
      period: "weekly",
      topic: "agents",
      limit: 12,
      page: 2,
      pageSize: 12
    })
  })

  it("returns community signal details", async () => {
    vi.mocked(getCommunitySignal).mockResolvedValueOnce({
      signal: {
        id: "signal-1",
        slug: "signal-1",
        source: "hackernews",
        title: "Agent memory",
        url: "https://news.ycombinator.com/item?id=1",
        summary: "Public discussion",
        postedAt: "2026-05-25T00:00:00Z",
        collectedAt: "2026-05-25T00:00:00Z",
        sentiment: "mixed",
        topics: ["agents"],
        entities: [],
        heatScore: 80,
        controversyScore: 40,
        adoptionScore: 30,
        relatedPaperIds: [],
        relatedProjectIds: [],
        relatedNewsIds: []
      },
      relatedPapers: [],
      relatedProjects: [],
      relatedNews: [],
      evidenceLinks: [],
      clusters: [],
      notices: []
    })

    const response = await getCommunitySignalRoute(new NextRequest("http://localhost/api/community/signals/signal-1"), {
      params: Promise.resolve({ id: "signal-1" })
    })
    const payload = await response.json()

    expect(response.status).toBe(200)
    expect(payload.success).toBe(true)
    expect(payload.data.signal.title).toBe("Agent memory")
  })

  it("returns not found for missing community signal details", async () => {
    vi.mocked(getCommunitySignal).mockResolvedValueOnce(undefined)

    const response = await getCommunitySignalRoute(new NextRequest("http://localhost/api/community/signals/missing"), {
      params: Promise.resolve({ id: "missing" })
    })
    const payload = await response.json()

    expect(response.status).toBe(404)
    expect(payload.success).toBe(false)
    expect(payload.error.code).toBe("community_signal_not_found")
  })

  it("returns not found for missing community topic details", async () => {
    vi.mocked(getCommunityTopic).mockResolvedValueOnce(undefined)

    const response = await getCommunityTopicRoute(new NextRequest("http://localhost/api/community/topics/missing"), {
      params: Promise.resolve({ slug: "missing" })
    })
    const payload = await response.json()

    expect(response.status).toBe(404)
    expect(payload.success).toBe(false)
    expect(payload.error.code).toBe("community_topic_not_found")
  })
})
