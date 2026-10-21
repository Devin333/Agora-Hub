import { PREVIEW_PAPERS } from "./data"
import type { LibraryView, PreviewState, PreviewTheme, ReaderTab } from "./types"

export const DEFAULT_PREVIEW_STATE: PreviewState = {
  theme: "light",
  paperId: "memgpt",
  view: "all",
  tab: "overview",
  focus: false,
  assistant: false,
}

const PAPER_IDS: ReadonlySet<string> = new Set(PREVIEW_PAPERS.map((paper) => paper.id))
const LIBRARY_VIEWS = new Set<LibraryView>(["all", "unread", "saved"])
const READER_TABS = new Set<ReaderTab>(["overview", "notes", "details"])

function isPreviewTheme(value: string | null): value is PreviewTheme {
  return value === "light" || value === "dark"
}

function isLibraryView(value: string | null): value is LibraryView {
  return value !== null && LIBRARY_VIEWS.has(value as LibraryView)
}

function isReaderTab(value: string | null): value is ReaderTab {
  return value !== null && READER_TABS.has(value as ReaderTab)
}

function parseBoolean(value: string | null): boolean {
  return value === "true"
}

export function parsePreviewState(search: string): PreviewState {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search)
  const paperId = params.get("paper")

  return {
    theme: isPreviewTheme(params.get("theme")) ? (params.get("theme") as PreviewTheme) : DEFAULT_PREVIEW_STATE.theme,
    paperId: paperId !== null && PAPER_IDS.has(paperId) ? paperId : DEFAULT_PREVIEW_STATE.paperId,
    view: isLibraryView(params.get("view")) ? (params.get("view") as LibraryView) : DEFAULT_PREVIEW_STATE.view,
    tab: isReaderTab(params.get("tab")) ? (params.get("tab") as ReaderTab) : DEFAULT_PREVIEW_STATE.tab,
    focus: parseBoolean(params.get("focus")),
    assistant: parseBoolean(params.get("assistant")),
  }
}

export function createPreviewHref(state: PreviewState): string {
  const params = new URLSearchParams({
    theme: state.theme,
    paper: PAPER_IDS.has(state.paperId) ? state.paperId : DEFAULT_PREVIEW_STATE.paperId,
    view: state.view,
    tab: state.tab,
    focus: String(state.focus),
    assistant: String(state.assistant),
  })

  return `/paper-agent-lab?${params.toString()}`
}

