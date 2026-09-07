export type Locale = "zh" | "en"

export type PaperModuleSection =
  | "trending_papers"
  | "paper_detail"
  | "tasks"
  | "task_detail"
  | "methods"
  | "method_detail"

export type PaperModuleRoute =
  | "/papers"
  | `/papers/${string}`
  | "/papers/tasks"
  | `/papers/tasks/${string}`
  | "/papers/methods"
  | `/papers/methods/${string}`
  | `/papers/${string}/read`

export type PaperSort = "trending" | "relevance" | "newest" | "most_cited"
export type PaperPeriod = "daily" | "weekly" | "monthly" | "all"
export type PaperDataState = "ready" | "degraded" | "empty"
export type ReadingStatus = "unread" | "reading" | "finished"

export interface CurrentUser {
  userId: string
  username: string
  role: string
  createdAt?: string
  updatedAt?: string
}

export interface AuthSession {
  user: CurrentUser
  sessionId: string
  expiresAt: string
  initialized?: boolean
}

export interface PaperUserState {
  userId: string
  paperId: string
  favorite: boolean
  subscribed: boolean
  readingStatus: ReadingStatus
  currentPage?: number
  progressPercent: number
  lastReadAt?: string
  updatedAt: string
}

export type PaperReaderNoteKind = "bookmark" | "highlight" | "note"
export type PaperReaderNoteColor = "yellow" | "green" | "blue" | "pink"

export interface PaperReaderNoteRect {
  left: number
  top: number
  width: number
  height: number
}

export interface PaperReaderNoteAnchor {
  pageNumber: number
  quote?: string
  rects?: PaperReaderNoteRect[]
  textStart?: number
  textEnd?: number
}

export interface PaperReaderNote {
  noteId: string
  userId: string
  paperId: string
  kind: PaperReaderNoteKind
  pageNumber: number
  color: PaperReaderNoteColor
  quote?: string
  noteText?: string
  label?: string
  anchor?: PaperReaderNoteAnchor
  createdAt: string
  updatedAt: string
}

export interface PaperReaderNoteCreate {
  kind: PaperReaderNoteKind
  pageNumber: number
  color?: PaperReaderNoteColor
  quote?: string
  noteText?: string
  label?: string
  anchor?: PaperReaderNoteAnchor
}

export interface PaperReaderNotePatch {
  color?: PaperReaderNoteColor
  quote?: string
  noteText?: string
  label?: string
  anchor?: PaperReaderNoteAnchor
}

export type ReaderTargetType = "text_selection" | "paragraph" | "figure" | "table" | "equation"

export type ReaderEventType =
  | "selection_created"
  | "selection_discarded"
  | "selection_updated"
  | "note_updated"
  | "explanation_generated"
  | "example_generated"
  | "confusion_marked"
  | "confusion_unmarked"
  | "reader_settings_changed"
  | "drawer_resized"
  | "toc_navigated"
  | "reader_progress_sampled"
  | "figure_explanation_requested"
  | "figure_explanation_generated"
  | "table_explanation_requested"
  | "table_explanation_generated"

export interface ReaderBlockTarget {
  targetType: ReaderTargetType
  blockId?: string
  assetId?: string
  sectionId?: string
  paragraphId?: string
  pageNumber?: number
  sourceBox?: Record<string, number>
  metadata?: Record<string, unknown>
}

export interface ReaderEvent {
  eventId: string
  type: ReaderEventType
  eventType: ReaderEventType
  userId: string
  paperId: string
  selectionId?: string
  target?: ReaderBlockTarget
  sectionId?: string
  paragraphId?: string
  selectedText?: string
  surroundingText?: string
  payload?: Record<string, unknown>
  createdAt: string
}

export type ReaderSelectionStatus = "temp" | "has_note" | "explained" | "exampled" | "confused"

export interface ReaderSelection {
  selectionId: string
  id: string
  userId: string
  paperId: string
  target: ReaderBlockTarget
  sectionId?: string
  sectionTitle?: string
  paragraphId?: string
  selectedText: string
  surroundingText: string
  noteText?: string
  explainQuestion?: string
  exampleQuestion?: string
  explained: boolean
  exampled: boolean
  confused: boolean
  status: ReaderSelectionStatus
  createdAt: string
  updatedAt: string
}

export interface ReaderMaterialSummary {
  paperId: string
  userId: string
  selections: ReaderSelection[]
  events: ReaderEvent[]
  stats: {
    noteCount: number
    explainedCount: number
    exampledCount: number
    confusedCount: number
    materialCount: number
  }
}

export interface ReaderFeedbackIngestStatus {
  queued: boolean
  enqueued?: {
    message_id: string
    task_id: string
    task_type: string
    queue_name: string
    status: string
  }
  reason?: string
}

export interface ReaderEventRecordResult {
  event: ReaderEvent
  selection: ReaderSelection | null
  materials: ReaderMaterialSummary
  feedbackIngest?: ReaderFeedbackIngestStatus
}

export interface ReaderEventCreate {
  type: ReaderEventType
  selectionId?: string
  target?: ReaderBlockTarget
  sectionId?: string
  paragraphId?: string
  selectedText?: string
  surroundingText?: string
  payload?: Record<string, unknown>
}

export interface ReaderSelectionCreate {
  selectionId?: string
  target?: ReaderBlockTarget
  sectionId?: string
  paragraphId?: string
  selectedText?: string
  surroundingText?: string
  payload?: Record<string, unknown>
}

export interface ReaderSelectionPatch {
  noteText?: string | null
  explained?: boolean
  exampled?: boolean
  confused?: boolean
  explainQuestion?: string
  exampleQuestion?: string
  question?: string
  answer?: string
  example?: string
}

export interface TaskRef {
  id: string
  slug: string
  name: string
  nameZh?: string
  group?: string
  confidence?: number
  evidence?: string
}

export interface MethodRef {
  id: string
  slug: string
  name: string
  nameZh?: string
  area?: string
  confidence?: number
  evidence?: string
}

export interface BenchmarkRef {
  id: string
  slug: string
  name: string
  category?: string
}

export interface PaperImplementation {
  id: string
  name: string
  repoUrl: string
  provider?: string
  githubStars?: number
}

export interface PaperBenchmarkResult {
  id: string
  name: string
  category?: string
  metric?: string
  value?: string | number
  taskSlug?: string
  url?: string
  confidence?: number
  evidence?: string
}

export interface PaperAISummary {
  paperId: string
  locale: Locale
  modelRoute: string
  abstractHash: string
  summary: string
  keyInsights: string[]
  limitations: string[]
  contributions?: string[]
  methodSummary?: string
  experimentSummary?: string
  engineeringRelevance?: string
  readingDifficulty?: "low" | "medium" | "high"
  recommendedAudience?: string[]
  summarySchemaVersion?: string
  generatedAt: string
  cached: boolean
}

export interface PaperSourceRef {
  sourceId?: string
  sourceName?: string
  sourceType?: string
  url?: string
  externalId?: string
  title?: string
}

export interface PaperEvidenceRef extends PaperSourceRef {
  evidenceId?: string
  summary?: string
  quote?: string
}

export interface Paper {
  id: string
  slug: string
  title: string
  titleZh?: string
  abstractSnippet: string
  abstractSnippetZh?: string
  authors: string[]
  publishedAt: string
  venue?: string
  citationCount?: number
  citationDoi?: string
  githubMomentum?: number
  thumbnailUrl?: string
  tags: string[]
  taskRefs: TaskRef[]
  methodRefs: MethodRef[]
  githubStars?: number
  starsPerHour?: number
  newsroomHeatScore?: number
  arxivId?: string
  paperUrl?: string
  arxivUrl?: string
  pdfUrl?: string
  repoUrl?: string
  projectUrl?: string
  implementations?: PaperImplementation[]
  benchmarks?: PaperBenchmarkResult[]
  aiSummary?: PaperAISummary
  evidenceRefs?: PaperEvidenceRef[]
  sourceRefs?: PaperSourceRef[]
  userState?: PaperUserState
  isPublished: boolean
}

export interface PaperListResult {
  source?: string
  query: string
  period: PaperPeriod
  sort: PaperSort
  task?: string
  method?: string
  topic?: string
  from?: string
  to?: string
  collectedAt?: string
  earliestPublishedAt?: string
  latestPublishedAt?: string
  paper_count: number
  total_count: number
  source_count: number
  limit: number
  offset: number
  has_next?: boolean
  dataState?: PaperDataState
  notices?: string[]
  papers: Paper[]
}

export interface PaperSection {
  id: string
  paperId: string
  title: string
  level: number
  pageStart?: number
  pageEnd?: number
  textExcerpt: string
  summary?: string
  metadata?: Record<string, unknown>
  sectionType:
    | "abstract"
    | "summary"
    | "contribution"
    | "introduction"
    | "related_work"
    | "method"
    | "experiment"
    | "result"
    | "limitation"
    | "implementation"
    | "benchmark"
    | "evidence"
    | "conclusion"
    | "appendix"
    | "unknown"
}

export interface PaperReaderQuality {
  paperId: string
  pdfAvailable: boolean
  textExtracted: boolean
  summaryAvailable: boolean
  implementationVerified: boolean
  benchmarkVerified: boolean
  evidenceCoverage: number
  lastUpdatedAt?: string | null
}

export interface RelatedPaper {
  id: string
  title: string
  slug: string
  relationReason: string
  score: number
  venue?: string
  publishedAt?: string
  paperUrl?: string
}

export interface RelatedProject {
  id: string
  name: string
  url?: string
  sourceType: "implementation" | "repository" | "project" | string
  relationReason: string
  score: number
  githubStars?: number
}

export interface RelatedNews {
  id: string
  title: string
  url?: string
  sourceType: string
  relationReason: string
  score: number
  summary?: string
}

export interface RelatedCommunitySignal {
  id: string
  title: string
  url?: string
  sourceType: string
  relationReason: string
  score: number
  comments?: number
}

export interface PaperSectionList {
  sections: PaperSection[]
}

export interface RelatedPaperList {
  relatedPapers: RelatedPaper[]
}

export interface PaperGraphNode {
  id: string
  type: "paper" | "project" | "news" | string
  label: string
  slug?: string
  url?: string
  sourceType?: string
  score?: number
}

export interface PaperGraphEdge {
  id: string
  source: string
  target: string
  type: "related" | string
  relationReason: string
  score: number
}

export interface PaperRelationGraph {
  paperId: string
  nodes: PaperGraphNode[]
  edges: PaperGraphEdge[]
}

export interface PaperReaderPayload {
  paper: Paper
  sections: PaperSection[]
  aiSummary: PaperAISummary | null
  readerNotes: PaperReaderNote[]
  relatedPapers: RelatedPaper[]
  relatedProjects: RelatedProject[]
  relatedNews: RelatedNews[]
  quality: PaperReaderQuality
}

export interface PaperReaderCitation {
  id: string
  label: string
  sourceType: "section" | "evidence" | "source" | string
  sectionId?: string
  evidenceId?: string
  sourceId?: string
  textExcerpt?: string
  url?: string
}

export interface PaperReaderAnswer {
  paperId: string
  locale: Locale
  question: string
  answer: string
  citations: PaperReaderCitation[]
  confidence: number
  generatedAt: string
  cached: boolean
}

export interface PaperTask {
  id: string
  slug: string
  name: string
  nameZh?: string
  group:
    | "agents"
    | "language-models"
    | "reasoning"
    | "multimodal"
    | "computer-vision"
    | "speech-audio"
    | "code-ai"
    | "robotics-embodied"
    | "retrieval-knowledge"
    | "data-evaluation"
    | "systems-infra"
    | "security-safety"
    | "ai-for-science"
    | "human-ai-interaction"
    | string
  description: string
  descriptionZh?: string
  paperCount: number
  benchmarkCount: number
  methodCount: number
  trendSignal?: string
  sisterTasks: TaskRef[]
  commonMethods: MethodRef[]
  latestPaperIds?: string[]
  implementationCount?: number
}

export interface PaperMethod {
  id: string
  slug: string
  name: string
  nameZh?: string
  description: string
  descriptionZh?: string
  paperCount: number
  taskCount: number
  implementationCount?: number
  area: string
  relatedTasks: TaskRef[]
  relatedMethods: MethodRef[]
  commonBenchmarks?: BenchmarkRef[]
  representativePaperIds?: string[]
  relatedProjectIds?: string[]
}

export interface Benchmark {
  id: string
  slug: string
  name: string
  category?: string
  taskSlug?: string
  methodSlug?: string
  entryCount: number
  metric?: string
  bestValue?: string | number
}
