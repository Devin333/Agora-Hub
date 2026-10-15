import {
  fallbackBoardDefinitions,
  fallbackOutputForBoard,
  fallbackSampleItems,
  fallbackSummaryForBoard,
  STUDIO_BOARD_TYPES
} from "@/features/studio/boards/lib/board-fallback-data"
import type {
  StudioBoardCard,
  StudioBoardDefinition,
  StudioBoardDetailViewModel,
  StudioBoardInsight,
  StudioBoardListViewModel,
  StudioBoardObjectRef,
  StudioBoardOutputViewModel,
  StudioBoardQualitySummary,
  StudioBoardSection,
  StudioBoardStats,
  StudioBoardSummary,
  StudioBoardType
} from "@/types/board"

type ApiBoardDefinition = {
  board_type?: unknown
  name?: unknown
  description?: unknown
  signal_types?: unknown
  visible_sections?: unknown
  default_time_window_hours?: unknown
  enabled?: unknown
}

type ApiBoardOutput = {
  board_type?: unknown
  cards?: unknown
  insights?: unknown
  detail_pages?: unknown
  sections?: unknown
  stats?: unknown
  metadata?: unknown
  radar_items?: unknown
  generated_at?: unknown
  quality_summary?: unknown
}

export function isStudioBoardType(value: unknown): value is StudioBoardType {
  return typeof value === "string" && (STUDIO_BOARD_TYPES as string[]).includes(value)
}

export function adaptBoardList(payload: unknown, options: { fallbackReason?: string } = {}): StudioBoardListViewModel {
  const apiBoards = readBoardArray(payload)
  const notices: string[] = []
  const definitions = { ...fallbackBoardDefinitions }

  if (options.fallbackReason) {
    notices.push(options.fallbackReason)
  }

  if (!apiBoards.length) {
    notices.push("Board definitions are using deterministic fallback data.")
  }

  for (const raw of apiBoards) {
    const board = raw as ApiBoardDefinition
    const boardType = normalizeBoardType(board.board_type)
    if (!boardType) {
      notices.push("An unknown board definition was ignored.")
      continue
    }
    definitions[boardType] = adaptBoardDefinition(board, definitions[boardType])
  }

  const summaries = STUDIO_BOARD_TYPES.map((boardType) =>
    summaryFromDefinition(definitions[boardType], {
      status: apiBoards.length ? "ready" : fallbackSummaryForBoard(boardType).status,
      notices: [
        ...definitions[boardType].notices,
        ...(apiBoards.length ? [] : fallbackSummaryForBoard(boardType).notices)
      ]
    })
  )

  return { summaries, definitions, notices: uniqueStrings(notices) }
}

export function adaptBoardOutput(
  boardType: StudioBoardType,
  payload: unknown,
  options: { fallbackReason?: string } = {}
): StudioBoardOutputViewModel {
  if (!isObject(payload)) {
    const fallback = fallbackOutputForBoard(boardType)
    return {
      ...fallback,
      notices: uniqueStrings([
        options.fallbackReason ?? "Board output payload was unavailable; fallback output is shown.",
        ...fallback.notices
      ])
    }
  }

  const raw = payload as ApiBoardOutput
  const definition = fallbackBoardDefinitions[boardType]
  const cards = readArray(raw.cards).map((card, index) => adaptCard(boardType, card, index))
  const insights = readArray(raw.insights).map((insight, index) => adaptInsight(insight, index))
  const detailPages = readArray(raw.detail_pages).map((page, index) => adaptDetailPage(page, index))
  const sections = readArray(raw.sections).map((section, index) => adaptSection(section, index))
  const stats = adaptStats(raw.stats, {
    cardCount: cards.length,
    detailPageCount: detailPages.length,
    insightCount: insights.length,
    radarItemCount: readArray(raw.radar_items).length
  })
  const notices: string[] = []

  if (options.fallbackReason) notices.push(options.fallbackReason)
  const quality = adaptQuality(raw.quality_summary, cards, stats)
  if (quality.source === "derived") {
    notices.push("BoardOutput did not include top-level quality_summary; quality is derived from card scores and stats.")
  }

  const metadata = isObject(raw.metadata) ? raw.metadata : {}
  const report = isObject(metadata.report) ? metadata.report : undefined

  return {
    boardType,
    title: readString(metadata.board_name) ?? definition.title,
    description: definition.description,
    generatedAt: readString(raw.generated_at),
    cards,
    insights,
    detailPages,
    sections,
    stats,
    quality,
    notices: uniqueStrings(notices),
    crossBoard: boardType === "cross_board" ? adaptCrossBoard(cards, insights, raw, report) : undefined
  }
}

export function buildBoardDetailViewModel(
  boardTypeValue: string,
  list: StudioBoardListViewModel,
  output?: StudioBoardOutputViewModel
): StudioBoardDetailViewModel {
  const boardType = isStudioBoardType(boardTypeValue) ? boardTypeValue : "cross_board"
  const unknownNotice = isStudioBoardType(boardTypeValue)
    ? []
    : [`Unknown board type "${boardTypeValue}" requested; Cross Board partial structure is shown.`]
  const definition = list.definitions[boardType]
  const summary =
    list.summaries.find((item) => item.boardType === boardType) ??
    summaryFromDefinition(definition, { status: "partial", notices: unknownNotice })

  return {
    summary: {
      ...summary,
      status: unknownNotice.length ? "partial" : summary.status,
      notices: uniqueStrings([...summary.notices, ...unknownNotice])
    },
    definition,
    output: output ?? fallbackOutputForBoard(boardType),
    sampleItemsJson: JSON.stringify(fallbackSampleItems[boardType], null, 2),
    notices: uniqueStrings([...unknownNotice, ...list.notices])
  }
}

export function summaryFromDefinition(
  definition: StudioBoardDefinition,
  options: { status?: StudioBoardSummary["status"]; notices?: string[] } = {}
): StudioBoardSummary {
  const fallbackSummary = fallbackSummaryForBoard(definition.boardType)
  return {
    boardType: definition.boardType,
    title: definition.title,
    description: definition.description,
    status: options.status ?? (definition.enabled ? "ready" : "partial"),
    lastRunId: fallbackSummary.lastRunId,
    qualityScore: fallbackSummary.qualityScore,
    cardCount: fallbackSummary.cardCount,
    insightCount: fallbackSummary.insightCount,
    notices: uniqueStrings(options.notices ?? definition.notices)
  }
}

function adaptBoardDefinition(raw: ApiBoardDefinition, fallback: StudioBoardDefinition): StudioBoardDefinition {
  return {
    boardType: fallback.boardType,
    title: readString(raw.name) ?? fallback.title,
    description: readString(raw.description) ?? fallback.description,
    inputObject: inputObjectForDefinition(raw, fallback),
    outputObject: fallback.outputObject,
    signalTypes: readStringArray(raw.signal_types, fallback.signalTypes),
    visibleSections: readStringArray(raw.visible_sections, fallback.visibleSections),
    defaultTimeWindowHours: readNumber(raw.default_time_window_hours) ?? fallback.defaultTimeWindowHours,
    enabled: typeof raw.enabled === "boolean" ? raw.enabled : fallback.enabled,
    notices: fallback.boardType === "cross_board" ? fallback.notices : []
  }
}

function inputObjectForDefinition(raw: ApiBoardDefinition, fallback: StudioBoardDefinition): string {
  const signalTypes = readStringArray(raw.signal_types, fallback.signalTypes)
  if (!signalTypes.length) return fallback.inputObject
  return `${signalTypes.join(", ")} signals`
}

function adaptCard(boardType: StudioBoardType, raw: unknown, index: number): StudioBoardCard {
  const card = isObject(raw) ? raw : {}
  return {
    id: readString(card.card_id) ?? `board-card-${index + 1}`,
    boardType,
    title: readString(card.title) ?? `Board card ${index + 1}`,
    subtitle: readString(card.subtitle),
    summary: readString(card.summary) ?? "No summary was provided.",
    badges: readArray(card.badges).map(adaptBadge),
    metrics: readArray(card.metrics).map(adaptMetric),
    relatedRefs: readArray(card.related_refs).map(adaptObjectRef),
    score: scoreToPercent(card.score),
    confidence: scoreToPercent(card.confidence),
    rankingReason: readString(card.ranking_reason),
    publishedAt: readString(card.published_at)
  }
}

function adaptInsight(raw: unknown, index: number): StudioBoardInsight {
  const insight = isObject(raw) ? raw : {}
  return {
    id: readString(insight.insight_id) ?? `board-insight-${index + 1}`,
    title: readString(insight.title) ?? `Board insight ${index + 1}`,
    summary: readString(insight.summary) ?? "No insight summary was provided.",
    insightType: readString(insight.insight_type),
    relatedRefs: readArray(insight.related_object_refs).map(adaptObjectRef),
    confidence: scoreToPercent(insight.confidence),
    importance: scoreToPercent(insight.importance)
  }
}

function adaptDetailPage(raw: unknown, index: number) {
  const page = isObject(raw) ? raw : {}
  return {
    id: readString(page.page_id) ?? `detail-page-${index + 1}`,
    title: readString(page.title) ?? `Detail page ${index + 1}`,
    summary: readString(page.summary) ?? "No detail summary was provided.",
    sectionCount: readArray(page.sections).length
  }
}

function adaptSection(raw: unknown, index: number): StudioBoardSection {
  const section = isObject(raw) ? raw : {}
  return {
    title: readString(section.title) ?? `Section ${index + 1}`,
    content: readString(section.content),
    metricCount: readArray(section.metrics).length,
    cardCount: readArray(section.cards).length,
    insightCount: readArray(section.insights).length
  }
}

function adaptStats(
  raw: unknown,
  fallback: Pick<StudioBoardStats, "cardCount" | "detailPageCount" | "insightCount" | "radarItemCount">
): StudioBoardStats {
  const stats = isObject(raw) ? raw : {}
  return {
    signalCount: readNumber(stats.signal_count) ?? 0,
    cardCount: readNumber(stats.card_count) ?? fallback.cardCount,
    detailPageCount: readNumber(stats.detail_page_count) ?? fallback.detailPageCount,
    insightCount: readNumber(stats.insight_count) ?? fallback.insightCount,
    relationCount: readNumber(stats.relation_count) ?? 0,
    radarItemCount: readNumber(stats.radar_item_count) ?? fallback.radarItemCount
  }
}

function adaptQuality(raw: unknown, cards: StudioBoardCard[], stats: StudioBoardStats): StudioBoardQualitySummary {
  if (isObject(raw)) {
    const score = scoreToPercent(raw.score)
    return {
      status: score !== undefined && score >= 75 ? "ready" : "partial",
      score,
      label: readString(raw.status) ?? "API quality",
      source: "api",
      checks: readArray(raw.checks).map((check, index) => {
        if (!isObject(check)) return `quality-check-${index + 1}`
        return readString(check.check_type) ?? readString(check.name) ?? `quality-check-${index + 1}`
      })
    }
  }

  const scoreValues = cards.map((card) => card.score).filter((value): value is number => value !== undefined)
  const score = scoreValues.length
    ? Math.round(scoreValues.reduce((total, value) => total + value, 0) / scoreValues.length)
    : stats.cardCount > 0
      ? 70
      : undefined
  return {
    status: "partial",
    score,
    label: "Derived quality",
    source: "derived",
    checks: ["card-count", "insight-count", "relation-count"]
  }
}

function adaptCrossBoard(
  cards: StudioBoardCard[],
  insights: StudioBoardInsight[],
  output: ApiBoardOutput,
  report: Record<string, unknown> | undefined
) {
  const relatedLabels = cards.flatMap((card) => card.relatedRefs.map((ref) => ref.label ?? ref.objectId))
  const sharedEntities = uniqueStrings(relatedLabels).slice(0, 6)
  const insightTitles = insights.map((insight) => insight.title)
  const radarItems = readArray(output.radar_items)
    .map((item, index) => (isObject(item) ? readString(item.title) ?? readString(item.name) : undefined) ?? `Radar item ${index + 1}`)
    .slice(0, 4)
  const notices = ["Dedicated cross-board graph/conflict endpoint data is not present; this panel uses BoardOutput fields."]

  return {
    associations: insightTitles.length ? insightTitles.slice(0, 4) : ["Associations will populate from cross-board insights."],
    trendPaths: radarItems.length ? radarItems : ["AI News -> Project Radar -> Paper Radar -> Community Pulse"],
    sharedEntities: sharedEntities.length ? sharedEntities : ["Shared entities unavailable in current output."],
    conflictSignals: insights
      .filter((insight) => `${insight.insightType ?? ""} ${insight.title}`.toLowerCase().includes("conflict"))
      .map((insight) => insight.title)
      .concat("Conflict signals require dedicated graph metadata.")
      .slice(0, 4),
    reportTitle: readString(report?.title),
    reportSummary: readString(report?.summary),
    notices
  }
}

function adaptBadge(raw: unknown) {
  const badge = isObject(raw) ? raw : {}
  return {
    label: readString(badge.label) ?? "badge",
    tone: readString(badge.tone),
    value: readString(badge.value)
  }
}

function adaptMetric(raw: unknown) {
  const metric = isObject(raw) ? raw : {}
  return {
    label: readString(metric.label) ?? "Metric",
    value: readString(metric.value) ?? readNumber(metric.value) ?? "n/a",
    unit: readString(metric.unit)
  }
}

function adaptObjectRef(raw: unknown): StudioBoardObjectRef {
  const ref = isObject(raw) ? raw : {}
  return {
    objectType: readString(ref.object_type) ?? "object",
    objectId: readString(ref.object_id) ?? "unknown",
    label: readString(ref.label)
  }
}

function readBoardArray(payload: unknown): unknown[] {
  if (isObject(payload) && Array.isArray(payload.boards)) return payload.boards
  if (Array.isArray(payload)) return payload
  return []
}

function normalizeBoardType(value: unknown): StudioBoardType | undefined {
  const text = readString(value)?.replaceAll("-", "_")
  return isStudioBoardType(text) ? text : undefined
}

function scoreToPercent(value: unknown): number | undefined {
  const rawValue = isObject(value) && "value" in value ? value.value : value
  const number = readNumber(rawValue)
  if (number === undefined) return undefined
  return Math.round(number <= 1 ? number * 100 : number)
}

function readString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined
}

function readNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined
}

function readArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function readStringArray(value: unknown, fallback: string[]): string[] {
  if (!Array.isArray(value)) return fallback
  const strings = value.filter((item): item is string => typeof item === "string" && Boolean(item.trim()))
  return strings.length ? strings : fallback
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))]
}
