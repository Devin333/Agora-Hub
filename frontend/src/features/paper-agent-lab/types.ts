export type PreviewTheme = "light" | "dark"
export type LibraryView = "all" | "unread" | "saved"
export type ReaderTab = "overview" | "notes" | "details"
export type PaperSort = "default" | "year-desc" | "title"
export type TopicId = "memory" | "retrieval" | "foundation"

export interface PreviewPaper {
  id: string
  title: string
  titleZh: string
  authors: string
  year: number
  venue: string
  arxivId: string
  sourceUrl: string
  topic: TopicId
  tags: readonly string[]
  summary: string
  keyPoints: readonly string[]
  sections: readonly {
    heading: string
    paragraphs: readonly string[]
  }[]
}

export interface PreviewState {
  theme: PreviewTheme
  paperId: string
  view: LibraryView
  tab: ReaderTab
  focus: boolean
  assistant: boolean
}

export interface PreviewQuestion {
  id: string
  prompt: string
  paperId: string
}
