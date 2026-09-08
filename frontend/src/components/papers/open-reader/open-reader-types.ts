import type { Locale, PaperReaderPayload, PaperSection } from "@/lib/papers/types"
import type { PaperBlock, PaperInlineSpan, PaperReference, PaperSourceRegion, PaperVisualAsset } from "@/lib/paper-reader/types"

export type ReaderTheme = "light" | "warm" | "dark"
export type ReaderAssistMode = "explain" | "example" | "materials"

export type ReaderSelectionStatus =
  | "temp"
  | "has_note"
  | "explained"
  | "exampled"
  | "confused"

export interface ReaderSettings {
  fontSize: number
  contentWidth: number
  theme: ReaderTheme
  drawerWidth: number
  layoutVersion: number
}

export interface ReaderParagraph {
  id: string
  paperId: string
  sectionId: string
  sectionTitle: string
  sectionType: PaperSection["sectionType"]
  sectionLevel: number
  sectionNumber?: string
  index: number
  text: string
  summary?: string
  pageStart?: number
  pageEnd?: number
  blockId?: string
  source?: PaperSourceRegion
  sourceOrder?: number
  inlineSpans?: PaperInlineSpan[]
}

export interface ReaderTocItem {
  id: string
  title: string
  sectionType: PaperSection["sectionType"]
  level: number
  sectionNumber?: string
  sourceOrder?: number
  paragraphCount: number
}

export interface OpenReaderVisualBlock {
  id: string
  paperId: string
  sectionId: string
  sectionTitle: string
  sectionType: PaperSection["sectionType"]
  sectionLevel: number
  sectionNumber?: string
  order: number
  block: PaperBlock
  asset?: PaperVisualAsset
  assets?: PaperVisualAsset[]
  source?: PaperSourceRegion
}

export interface OpenReaderVisualLayer {
  blocks: OpenReaderVisualBlock[]
  outline?: ReaderTocItem[]
  references?: PaperReference[]
}

export interface ReaderSelection {
  id: string
  paperId: string
  sectionId: string
  sectionTitle: string
  paragraphId: string
  blockId?: string
  source?: PaperSourceRegion
  pageNumber?: number
  selectedText: string
  surroundingText: string
  startOffset: number
  endOffset: number
  noteText: string
  explainQuestion: string
  explainAnswer?: string
  exampleQuestion: string
  exampleAnswer?: string
  explained: boolean
  exampled: boolean
  confused: boolean
  createdAt: string
  updatedAt: string
}

export type ReaderEventType =
  | "selection_created"
  | "selection_discarded"
  | "note_updated"
  | "explanation_generated"
  | "example_generated"
  | "confusion_marked"
  | "confusion_unmarked"
  | "reader_settings_changed"
  | "drawer_resized"
  | "toc_navigated"

export interface ReaderEvent {
  id: string
  type: ReaderEventType
  paperId: string
  selectionId?: string
  paragraphId?: string
  blockId?: string
  sectionId?: string
  pageNumber?: number
  source?: PaperSourceRegion
  selectedText?: string
  surroundingText?: string
  payload?: Record<string, unknown>
  createdAt: string
}

export interface ReaderMaterialSummary {
  paperId: string
  selections: ReaderSelection[]
  events: ReaderEvent[]
  stats: {
    noteCount: number
    explainedCount: number
    exampledCount: number
    confusedCount: number
  }
}

export interface OpenReaderPageProps {
  reader: PaperReaderPayload
  locale: Locale
  backHref?: string
  visualLayer?: OpenReaderVisualLayer
  workspace?: {
    fontSize: number
    sectionRequest?: { id: string; sequence: number }
    onSectionChange?: (id: string) => void
  }
}

export interface SelectionMenuState {
  selectionId: string
  x: number
  y: number
}

export interface NotePopoverState {
  selectionId: string
  x: number
  y: number
}

export interface DrawerState {
  mode: ReaderAssistMode
  selectionId?: string
}
