import { beforeEach, describe, expect, it, vi } from "vitest"
import { NextRequest } from "next/server"
import { GET as getEvidenceGraphRoute } from "@/app/api/evidence-graph/route"
import { GET as getEvidenceGraphNodeRoute } from "@/app/api/evidence-graph/nodes/[id]/route"
import { GET as getTopicTimelineRoute } from "@/app/api/topics/[topicId]/timeline/route"
import {
  getEvidenceGraphData,
  getEvidenceGraphNodeDetail,
  getEvidenceGraphTimeline,
} from "@/features/evidence-graph/evidence-graph-data"
import type { EvidenceGraphResponse } from "@/types/evidence-graph"

vi.mock("@/features/evidence-graph/evidence-graph-data", async () => {
  const actual = await vi.importActual<typeof import("@/features/evidence-graph/evidence-graph-data")>(
    "@/features/evidence-graph/evidence-graph-data"
  )
  return {
    ...actual,
    getEvidenceGraphData: vi.fn(),
    getEvidenceGraphNodeDetail: vi.fn(),
    getEvidenceGraphTimeline: vi.fn(),
  }
})

const graphResponse: EvidenceGraphResponse = {
  generatedAt: "2026-05-26T00:00:00Z",
  query: { topic: "Agent Memory", period: "weekly", depth: 3, limit: 12 },
  summary: {
    topicId: "topic:agent-memory",
    topicName: "Agent Memory",
    summary: "Agent Memory summary",
    trendScore: 80,
    evidenceScore: 70,
    confidenceScore: 75,
    paperCount: 1,
    projectCount: 0,
    newsCount: 0,
    communitySignalCount: 0,
    firstSeenAt: "2026-05-25T00:00:00Z",
    lastUpdatedAt: "2026-05-25T00:00:00Z",
    trajectory: "rising",
    keyEvidenceNodeIds: ["paper:agent-memory"],
  },
  nodes: [
    { id: "topic:agent-memory", type: "topic", title: "Agent Memory" },
    { id: "paper:agent-memory", type: "paper", title: "Agent Memory Paper" },
  ],
  edges: [{ id: "edge-1", sourceNodeId: "topic:agent-memory", targetNodeId: "paper:agent-memory", type: "same_topic", confidence: 0.8 }],
  timeline: [
    {
      id: "timeline-1",
      topicId: "topic:agent-memory",
      occurredAt: "2026-05-25T00:00:00Z",
      title: "Paper signal",
      summary: "Paper timeline",
      sourceCount: 1,
      nodeIds: ["paper:agent-memory"],
      importance: "high",
      board: "paper",
    },
  ],
  relatedReports: [],
  sourceStates: [],
  notices: [],
}

describe("evidence graph BFF routes", () => {
  beforeEach(() => {
    vi.mocked(getEvidenceGraphData).mockReset()
    vi.mocked(getEvidenceGraphNodeDetail).mockReset()
    vi.mocked(getEvidenceGraphTimeline).mockReset()
  })

  it("returns graph data with parsed query parameters", async () => {
    vi.mocked(getEvidenceGraphData).mockResolvedValueOnce(graphResponse)

    const response = await getEvidenceGraphRoute(
      new NextRequest("http://localhost/api/evidence-graph?topic=Agent%20Memory&period=weekly&nodeTypes=paper,news&depth=3&limit=12")
    )
    const payload = await response.json()

    expect(response.status).toBe(200)
    expect(payload.success).toBe(true)
    expect(getEvidenceGraphData).toHaveBeenCalledWith(
      expect.objectContaining({
        topic: "Agent Memory",
        period: "weekly",
        nodeTypes: ["paper", "news"],
        depth: 3,
        limit: 12,
      })
    )
  })

  it("returns node details and a 404 for missing nodes", async () => {
    vi.mocked(getEvidenceGraphNodeDetail).mockResolvedValueOnce({
      node: graphResponse.nodes[1],
      incomingEdges: graphResponse.edges,
      outgoingEdges: [],
      relatedNodes: [graphResponse.nodes[0]],
    })

    const found = await getEvidenceGraphNodeRoute(new NextRequest("http://localhost/api/evidence-graph/nodes/paper%3Aagent-memory"), {
      params: Promise.resolve({ id: "paper%3Aagent-memory" }),
    })
    expect(found.status).toBe(200)
    expect((await found.json()).data.node.id).toBe("paper:agent-memory")

    vi.mocked(getEvidenceGraphNodeDetail).mockResolvedValueOnce(null)
    const missing = await getEvidenceGraphNodeRoute(new NextRequest("http://localhost/api/evidence-graph/nodes/missing"), {
      params: Promise.resolve({ id: "missing" }),
    })
    expect(missing.status).toBe(404)
  })

  it("returns topic timeline items", async () => {
    vi.mocked(getEvidenceGraphTimeline).mockResolvedValueOnce({ items: graphResponse.timeline })

    const response = await getTopicTimelineRoute(new NextRequest("http://localhost/api/topics/topic%3Aagent-memory/timeline"), {
      params: Promise.resolve({ topicId: "topic%3Aagent-memory" }),
    })
    const payload = await response.json()

    expect(response.status).toBe(200)
    expect(payload.success).toBe(true)
    expect(payload.data.items).toHaveLength(1)
    expect(getEvidenceGraphTimeline).toHaveBeenCalledWith("topic:agent-memory", expect.any(Object))
  })
})
