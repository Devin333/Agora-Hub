import type {
  StudioBoardDefinition,
  StudioBoardOutputViewModel,
  StudioBoardSummary,
  StudioBoardType
} from "@/types/board"

export const STUDIO_BOARD_TYPES: StudioBoardType[] = [
  "ai_news",
  "project_radar",
  "paper_radar",
  "community_pulse",
  "cross_board"
]

export const fallbackBoardDefinitions: Record<StudioBoardType, StudioBoardDefinition> = {
  ai_news: {
    boardType: "ai_news",
    title: "AI News",
    description: "Fresh AI news and adoption signals.",
    inputObject: "AI news source items",
    outputObject: "Board cards, insights, detail pages, quality summary",
    signalTypes: ["ai_news"],
    visibleSections: ["top_signals", "technology_radar", "cross_board_insights"],
    defaultTimeWindowHours: 72,
    enabled: true,
    notices: []
  },
  project_radar: {
    boardType: "project_radar",
    title: "Project Radar",
    description: "High quality GitHub AI projects.",
    inputObject: "GitHub project signals",
    outputObject: "Project cards, related papers, quality summary",
    signalTypes: ["github_project"],
    visibleSections: ["top_projects", "technology_radar", "related_papers"],
    defaultTimeWindowHours: 336,
    enabled: true,
    notices: []
  },
  paper_radar: {
    boardType: "paper_radar",
    title: "Paper Radar",
    description: "Papers and emerging techniques.",
    inputObject: "Paper and arXiv signals",
    outputObject: "Paper cards, related projects, quality summary",
    signalTypes: ["paper"],
    visibleSections: ["top_papers", "technology_radar", "related_projects"],
    defaultTimeWindowHours: 720,
    enabled: true,
    notices: []
  },
  community_pulse: {
    boardType: "community_pulse",
    title: "Community Pulse",
    description: "Community discussion and sentiment.",
    inputObject: "Community discussion signals",
    outputObject: "Thread cards, community trends, quality summary",
    signalTypes: ["community_discussion"],
    visibleSections: ["top_threads", "community_trends", "cross_board_insights"],
    defaultTimeWindowHours: 168,
    enabled: true,
    notices: []
  },
  cross_board: {
    boardType: "cross_board",
    title: "Cross Board",
    description: "Cross-board intelligence and daily digest.",
    inputObject: "Mixed board signals",
    outputObject: "Cross-board associations, trend paths, conflicts, report entry",
    signalTypes: ["ai_news", "github_project", "paper", "community_discussion"],
    visibleSections: ["cross_board_insights", "technology_journeys", "daily_report"],
    defaultTimeWindowHours: 24,
    enabled: true,
    notices: ["Cross Board may show partial graph intelligence until dedicated graph endpoints are available."]
  }
}

export const fallbackSampleItems: Record<StudioBoardType, Record<string, unknown>[]> = {
  ai_news: [
    sampleItem("ai-news-1", "rss", "OpenAI Blog", "Agent Memory update", [
      "ai",
      "agent memory",
      "product_update"
    ])
  ],
  project_radar: [
    sampleItem("project-1", "github", "GitHub Trending", "Agent workflow runtime", [
      "github",
      "project",
      "agent memory"
    ])
  ],
  paper_radar: [
    sampleItem("paper-1", "arxiv", "arXiv cs.AI", "Agent memory evaluation paper", [
      "paper",
      "evaluation",
      "memory"
    ])
  ],
  community_pulse: [
    sampleItem("community-1", "hackernews", "Hacker News", "Teams compare agent memory systems", [
      "community",
      "discussion",
      "agent memory"
    ])
  ],
  cross_board: [
    sampleItem("ai-news-1", "rss", "OpenAI Blog", "Agent Memory update", ["ai", "agent memory"]),
    sampleItem("project-1", "github", "GitHub Trending", "Agent workflow runtime", ["github", "project"]),
    sampleItem("paper-1", "arxiv", "arXiv cs.AI", "Agent memory evaluation paper", ["paper", "evaluation"]),
    sampleItem("community-1", "hackernews", "Hacker News", "Teams compare agent memory systems", [
      "community",
      "discussion"
    ])
  ]
}

export function fallbackSummaryForBoard(boardType: StudioBoardType): StudioBoardSummary {
  const definition = fallbackBoardDefinitions[boardType]
  return {
    boardType,
    title: definition.title,
    description: definition.description,
    status: boardType === "cross_board" ? "partial" : "fallback",
    lastRunId: "pending-api-run",
    qualityScore: boardType === "cross_board" ? 78 : 82,
    cardCount: boardType === "cross_board" ? 4 : 1,
    insightCount: boardType === "cross_board" ? 3 : 1,
    notices: [
      "Fallback board definition is active until /api/v1/boards returns this board.",
      ...definition.notices
    ]
  }
}

export function fallbackOutputForBoard(boardType: StudioBoardType): StudioBoardOutputViewModel {
  const definition = fallbackBoardDefinitions[boardType]
  const cardTitle = boardType === "cross_board" ? "Agent Memory crosses news, projects, papers, and community" : definition.title
  const cards = [
    {
      id: `${boardType}-fallback-card`,
      boardType,
      title: cardTitle,
      subtitle: definition.inputObject,
      summary: `${definition.description} This fallback card preserves the board output structure while the API is unavailable.`,
      badges: [
        { label: boardType },
        { label: "fallback", tone: "warning" }
      ],
      metrics: [
        { label: "Signals", value: boardType === "cross_board" ? 4 : 1 },
        { label: "Relations", value: boardType === "cross_board" ? 6 : 2 }
      ],
      relatedRefs: [
        { objectType: "topic", objectId: "topic-agent-memory", label: "Agent Memory" },
        { objectType: "technology", objectId: "tech-workflow", label: "Workflow" }
      ],
      score: boardType === "cross_board" ? 78 : 82,
      confidence: 76,
      rankingReason: "Fallback ranking keeps the operations shape inspectable."
    }
  ]
  const insights =
    boardType === "cross_board"
      ? [
          {
            id: "cross-board-association",
            title: "Agent Memory appears across board surfaces",
            summary: "The same entity is present in news, project, paper, and community samples.",
            insightType: "cross_board_association",
            relatedRefs: cards[0].relatedRefs,
            confidence: 72,
            importance: 80
          },
          {
            id: "cross-board-conflict",
            title: "Quality signal remains partial",
            summary: "Dedicated conflict and graph intelligence endpoints are not attached to this view yet.",
            insightType: "conflict_signal",
            relatedRefs: cards[0].relatedRefs,
            confidence: 60,
            importance: 66
          }
        ]
      : [
          {
            id: `${boardType}-fallback-insight`,
            title: `${definition.title} sample insight`,
            summary: "This board can render insights, cards, detail pages, and derived quality from API output.",
            insightType: "board_sample",
            relatedRefs: cards[0].relatedRefs,
            confidence: 70,
            importance: 72
          }
        ]

  return {
    boardType,
    title: definition.title,
    description: definition.description,
    generatedAt: "2026-05-24T00:00:00Z",
    cards,
    insights,
    detailPages: [
      {
        id: `${boardType}-fallback-detail`,
        title: `${definition.title} Detail`,
        summary: "Fallback detail page with normalized board output structure.",
        sectionCount: 2
      }
    ],
    sections: [
      {
        title: "Cards",
        content: "Board cards are grouped by business object, not by reader news layout.",
        metricCount: 2,
        cardCount: cards.length,
        insightCount: 0
      }
    ],
    stats: {
      signalCount: boardType === "cross_board" ? 4 : 1,
      cardCount: cards.length,
      detailPageCount: 1,
      insightCount: insights.length,
      relationCount: boardType === "cross_board" ? 6 : 2,
      radarItemCount: boardType === "cross_board" ? 2 : 1
    },
    quality: {
      status: boardType === "cross_board" ? "partial" : "fallback",
      score: boardType === "cross_board" ? 78 : 82,
      label: boardType === "cross_board" ? "Partial graph quality" : "Fallback quality",
      source: "fallback",
      checks: ["fallback-board-output", "visible-fallback-notice"]
    },
    notices: ["API output is unavailable; deterministic fallback board output is shown.", ...definition.notices],
    crossBoard:
      boardType === "cross_board"
        ? {
            associations: ["Agent Memory links AI News, Project Radar, Paper Radar, and Community Pulse."],
            trendPaths: ["AI News -> Project Radar -> Community Pulse"],
            sharedEntities: ["Agent Memory", "Workflow"],
            conflictSignals: ["Graph conflict endpoint not available; showing partial signal."],
            reportTitle: "Cross Board Digest",
            reportSummary: "Fallback cross-board report entry for integrated review.",
            notices: ["Cross-board graph details are partial until dedicated endpoint data is available."]
          }
        : undefined
  }
}

function sampleItem(id: string, sourceType: string, sourceName: string, title: string, tags: string[]) {
  return {
    source_item_id: id,
    source_id: `${sourceType}-source`,
    source_name: sourceName,
    source_type: sourceType,
    title,
    summary: `${title} improves workflow orchestration and evidence quality.`,
    content: `${title} improves workflow orchestration and evidence quality.`,
    url: `https://example.com/${id}`,
    language: "en",
    authors: ["NewsRoom"],
    tags,
    fetched_at: "2026-05-19T01:00:00Z",
    published_at: "2026-05-19T00:00:00Z",
    metadata: {
      source_reliability: "high",
      source_authority_score: 0.9
    }
  }
}
