import { fireEvent, render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"
import { CommunityPulsePage } from "@/features/community/components/community-pulse-page"
import { CommunityTopicDetail } from "@/features/community/components/community-topic-detail"
import {
  buildCommunitySignalDetailResult,
  buildCommunitySignalListResult
} from "@/lib/community/community-signals"
import type { CommunityTopic, CommunityTopicDetail as CommunityTopicDetailType } from "@/types/community"

vi.mock("next/navigation", () => ({
  usePathname: () => "/community",
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

describe("Community Pulse UI", () => {
  it("renders an empty state when no community signals are available", () => {
    const result = buildCommunitySignalListResult([], [], {}, { source: "empty" })

    render(
      <CommunityPulsePage
        result={result}
        filters={{}}
        onChange={vi.fn()}
        onOpenSignal={vi.fn()}
        onCloseSignal={vi.fn()}
      />
    )

    expect(screen.getByText("No community signals")).toBeInTheDocument()
  })

  it("renders signal stream and forwards filter interactions", () => {
    const onChange = vi.fn()
    const onOpenSignal = vi.fn()
    const result = buildCommunitySignalListResult([sampleTopic()], [sampleDetail()], {}, { source: "artifact" })

    render(
      <CommunityPulsePage
        result={result}
        filters={{}}
        sourceQuestion="Agent 社区最近在讨论什么？"
        onChange={onChange}
        onOpenSignal={onOpenSignal}
        onCloseSignal={vi.fn()}
      />
    )

    expect(screen.getAllByText("Agent memory debate").length).toBeGreaterThan(0)
    expect(screen.getByText("Agent 社区最近在讨论什么？")).toBeInTheDocument()
    expect(screen.getByText("Hot Discussion")).toBeInTheDocument()
    expect(screen.getByText("Debate Cluster")).toBeInTheDocument()
    expect(screen.getAllByText("1 papers").length).toBeGreaterThan(0)

    fireEvent.click(screen.getByRole("button", { name: "Positive" }))
    expect(onChange).toHaveBeenCalledWith({ sentiment: "positive", cursor: undefined })

    fireEvent.change(screen.getByRole("textbox", { name: "Search community signals" }), {
      target: { value: "latency" }
    })
    fireEvent.click(screen.getByRole("button", { name: "Search" }))
    expect(onChange).toHaveBeenLastCalledWith({ q: "latency", cursor: undefined })

    fireEvent.click(screen.getByRole("button", { name: "Inspect signal" }))
    expect(onOpenSignal).toHaveBeenCalledWith("topic-1")
  })

  it("renders the signal detail drawer from public data", () => {
    const detail = buildCommunitySignalDetailResult([sampleTopic()], [sampleDetail()], "topic-1")
    expect(detail).toBeDefined()
    const onClose = vi.fn()
    const result = buildCommunitySignalListResult([sampleTopic()], [sampleDetail()], {}, { source: "artifact" })

    render(
      <CommunityPulsePage
        result={result}
        filters={{}}
        selectedSignal={detail}
        onChange={vi.fn()}
        onOpenSignal={vi.fn()}
        onCloseSignal={onClose}
      />
    )

    expect(screen.getByRole("dialog", { name: "Community signal detail" })).toBeInTheDocument()
    expect(screen.getByText("Evidence links")).toBeInTheDocument()
    expect(screen.getByText("Memory repo")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Close signal detail" }))
    expect(onClose).toHaveBeenCalled()
  })

  it("renders Community Pulse topic detail sections from public data", () => {
    render(<CommunityTopicDetail topic={sampleDetail()} />)

    expect(screen.getByText("Top discussions")).toBeInTheDocument()
    expect(screen.getByText("Representative comments")).toBeInTheDocument()
    expect(screen.getByText("Memory paper")).toBeInTheDocument()
    expect(screen.getByText("Open discussion")).toBeInTheDocument()
  })
})

function sampleTopic(): CommunityTopic {
  return {
    id: "topic-1",
    slug: "agent-memory-debate",
    title: "Agent memory debate",
    summary: "Developers discuss memory latency, reliability, and cost.",
    sourceType: "hackernews",
    sourceName: "Hacker News",
    sourceUrl: "https://news.ycombinator.com/item?id=1",
    publishedAt: "2026-05-24T00:00:00Z",
    lastActivityAt: "2026-05-25T00:00:00Z",
    sentiment: "mixed",
    heatScore: 88,
    controversyScore: 42,
    adoptionScore: 35,
    commentCount: 18,
    upvoteCount: 75,
    tags: ["agents"],
    relatedPapers: [{ id: "paper-1", title: "Memory paper" }],
    relatedProjects: [{ id: "project-1", name: "Memory repo" }],
    relatedNews: [{ id: "news-1", title: "Memory launch" }],
    evidenceRefs: [
      {
        id: "evidence-1",
        sourceName: "Hacker News",
        sourceType: "hackernews",
        url: "https://news.ycombinator.com/item?id=1",
        excerpt: "Public excerpt only."
      }
    ]
  }
}

function sampleDetail(): CommunityTopicDetailType {
  return {
    ...sampleTopic(),
    sourceDistribution: [{ sourceType: "hackernews", count: 1 }],
    topDiscussions: [
      {
        id: "discussion-1",
        title: "Agent memory debate",
        sourceType: "hackernews",
        sourceName: "Hacker News",
        url: "https://news.ycombinator.com/item?id=1",
        excerpt: "Public discussion excerpt.",
        publishedAt: "2026-05-24T00:00:00Z"
      }
    ],
    representativeComments: [
      {
        id: "comment-1",
        excerpt: "Latency is the main blocker.",
        sentiment: "mixed",
        sourceName: "Hacker News"
      }
    ],
    timeline: [
      {
        id: "timeline-1",
        label: "Published",
        timestamp: "2026-05-24T00:00:00Z"
      }
    ],
    notices: []
  }
}
