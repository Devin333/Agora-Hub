import type { Paper, PaperAISummary } from "@/lib/papers/types"

export type PaperDocumentStatus =
  | "not_compiled"
  | "queued"
  | "compiling"
  | "needs_review"
  | "compile_failed"
  | "review_failed"
  | "compiled"

export type PaperBlockType = "heading" | "paragraph" | "figure" | "table" | "equation"
export type PaperVisualAssetKind = "page" | "figure" | "table" | "equation"

export interface PaperSourceRegion {
  pageNumber: number
  bbox: {
    x0: number
    y0: number
    x1: number
    y1: number
  }
  pageWidth?: number
  pageHeight?: number
}

export type PaperInlineSpan =
  | {
      type: "text"
      text: string
      start: number
      end: number
    }
  | {
      type: "math"
      text: string
      latex: string
      displayMode?: boolean
      start: number
      end: number
    }
  | {
      type: "ref"
      text: string
      label?: string
      refKind?: "figure" | "table" | "section" | "equation" | "reference" | string
      targetBlockId?: string | null
      sectionId?: string | null
      display?: string
      start: number
      end: number
    }
  | {
      type: "citation"
      text: string
      citations: Array<{
        key: string
        number: number | null
        referenceId: string | null
        missing?: boolean
      }>
      start: number
      end: number
    }

export interface PaperReference {
  id: string
  key: string
  number: number
  label: string
  title?: string
  authors?: string[]
  year?: string
  venue?: string
  doi?: string
  url?: string
  text: string
  missing?: boolean
}

export interface PaperBlock {
  id: string
  paperId: string
  type: PaperBlockType
  text?: string
  level?: number
  pageNumber?: number
  sectionId?: string
  assetId?: string
  label?: string
  caption?: string
  source?: PaperSourceRegion
  metadata?: Record<string, unknown>
}

export interface PaperVisualAsset {
  assetId: string
  paperId: string
  kind: PaperVisualAssetKind
  fileName: string
  mimeType: string
  width: number
  height: number
  checksum: string
  pageNumber?: number
  label?: string
  caption?: string
  source?: PaperSourceRegion
  blankRatio?: number
  fileSize?: number
  metadata?: Record<string, unknown>
}

export interface PaperAssetManifest {
  paperId: string
  schemaVersion: string
  createdAt: string
  sourceHash: string
  sourcePdfFileName?: string
  provider?: string
  assets: PaperVisualAsset[]
}

export interface PaperCompileInfo {
  paperId: string
  status: PaperDocumentStatus
  provider: string
  sourceHash: string
  startedAt: string
  finishedAt?: string
  sourcePdfUrl?: string
  pageCount: number
  blockCount: number
  assetCount: number
  diagnostics: PaperDiagnostic[]
}

export interface PaperReviewReport {
  paperId: string
  verdict: "pass" | "fail" | "unavailable"
  reviewer: string
  createdAt: string
  summary: string
  findings: string[]
  risks: string[]
  suggestions: string[]
  modelRoute?: string
  raw?: Record<string, unknown>
}

export interface PaperDiagnostic {
  severity?: "info" | "warning" | "error" | string
  code: string
  message: string
  [key: string]: unknown
}

export interface PaperCompileStatusRecord {
  paperId: string
  status: PaperDocumentStatus
  updatedAt: string
  diagnostics: PaperDiagnostic[]
  compileInfo?: PaperCompileInfo | null
  reviewReport?: PaperReviewReport | null
  gateReport?: {
    passed?: boolean
    errors?: PaperDiagnostic[]
    warnings?: PaperDiagnostic[]
    [key: string]: unknown
  } | null
}

export interface PaperDocument {
  paperId: string
  schemaVersion: string
  status: PaperDocumentStatus
  title: string
  compiledAt: string
  sourceHash: string
  paper: Partial<Paper>
  outline: Array<{
    id: string
    title: string
    level: number
    pageNumber?: number
    blockId?: string
    sectionNumber?: string
  }>
  blocks: PaperBlock[]
  auxiliary?: Record<string, unknown>
}

export interface PaperDocumentAiPanel {
  summary?: PaperAISummary | null
  signals?: {
    abstractSnippet?: string
    methodRefs?: unknown[]
    taskRefs?: unknown[]
    benchmarks?: unknown[]
    implementations?: unknown[]
  }
  review?: PaperReviewReport | null
  diagnostics?: PaperDiagnostic[]
}

export interface PaperDocumentResponse {
  paper: Paper
  status: PaperCompileStatusRecord
  document: PaperDocument | null
  manifest: PaperAssetManifest | null
  ai?: PaperDocumentAiPanel
}

export interface PaperCompileTriggerResponse {
  enqueued: {
    message_id: string
    task_id: string
    task_type: string
    queue_name: string
    status: string
    paper_id?: string
    run_id?: string
    mode?: string
  }
}
