import type { AgentRun } from "@/types/agent"
import type { NewsItem } from "@/types/news"
import type { QualityGateSummary } from "@/types/quality"
import type { SourceHealth } from "@/types/source"
import { evidences, newsItems, reports, topics } from "@/lib/mock-data"

export const mockNewsItems = newsItems
export const mockNews = newsItems
export const mockTopics = topics
export const mockReports = reports
export const mockEvidence = evidences

export const mockAgentRuns: AgentRun[] = [
  {
    id: "run-20260523-reader-brief",
    agentName: "WriterAgent",
    workflowName: "daily-reader-intelligence-brief",
    profile: "reader-portal",
    status: "success",
    startedAt: "2026-05-23T08:12:00+08:00",
    finishedAt: "2026-05-23T08:31:00+08:00",
    durationMs: 1_140_000,
    durationSeconds: 1140,
    inputCount: 146,
    outputCount: 58,
    artifactCount: 7,
    qualityScore: 91,
    errorCount: 0,
    steps: [
      { id: "collect", label: "Collect sources", status: "success" },
      { id: "cluster", label: "Cluster topics", status: "success" },
      { id: "score", label: "Score quality", status: "success" },
      { id: "report", label: "Generate briefing", status: "success" }
    ]
  }
]

export const mockSources: SourceHealth[] = [
  {
    id: "source-official-ai",
    name: "Official AI Blog",
    type: "official_blog",
    status: "healthy",
    successRate: 99,
    lastCheckedAt: "2026-05-23T08:20:00+08:00"
  },
  {
    id: "source-github-trending",
    name: "GitHub Trending",
    type: "github",
    status: "healthy",
    successRate: 96,
    lastCheckedAt: "2026-05-23T08:18:00+08:00"
  },
  {
    id: "source-hn-ai",
    name: "Hacker News AI",
    type: "hackernews",
    status: "degraded",
    successRate: 82,
    lastCheckedAt: "2026-05-23T08:10:00+08:00"
  }
]

export const mockQualityResult: QualityGateSummary = {
  status: "passed",
  passedChecks: 11,
  totalChecks: 12,
  summary: "Citation coverage and source freshness passed; one community source is marked for review."
}

export function getEvidenceForNews(news: NewsItem) {
  const ids = new Set(news.evidenceIds ?? [])
  return evidences.filter((item) => ids.has(item.id))
}

export function resolveMockPath(path: string): unknown {
  const normalizedPath = path.split("?")[0]
  if (normalizedPath === "/news") return { items: newsItems, total: newsItems.length, page: 1, pageSize: 20, hasNext: false }
  if (normalizedPath.startsWith("/news/")) return { data: newsItems.find((item) => item.id === normalizedPath.split("/").pop()) }
  if (normalizedPath === "/topics") return { items: topics, total: topics.length, page: 1, pageSize: 20, hasNext: false }
  if (normalizedPath.startsWith("/topics/")) return { data: topics.find((item) => item.id === normalizedPath.split("/").pop()) ?? topics[0] }
  if (normalizedPath === "/reports") return { items: reports, total: reports.length, page: 1, pageSize: 20, hasNext: false }
  if (normalizedPath.startsWith("/reports/")) return { data: reports.find((item) => item.id === normalizedPath.split("/").pop()) ?? reports[0] }
  if (normalizedPath === "/studio/runs") return { items: mockAgentRuns, total: mockAgentRuns.length, page: 1, pageSize: 20, hasNext: false }
  if (normalizedPath.startsWith("/studio/runs/")) return { data: mockAgentRuns.find((item) => item.id === normalizedPath.split("/").pop()) ?? mockAgentRuns[0] }
  if (normalizedPath === "/studio/sources") return { items: mockSources, total: mockSources.length, page: 1, pageSize: 20, hasNext: false }
  if (normalizedPath === "/studio/quality") return mockQualityResult
  return undefined
}
