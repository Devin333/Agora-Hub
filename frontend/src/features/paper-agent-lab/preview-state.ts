import type { ResolvedTheme } from "@/lib/theme/appearance"
import { PREVIEW_PAPERS } from "./data"
import type { LibraryView, PreviewState, ReaderTab } from "./types"

export const DEFAULT_PREVIEW_STATE: PreviewState = {
  paperId: "memgpt",
  view: "all",
  tab: "overview",
  focus: false,
  assistant: false,
}

const PAPER_IDS: ReadonlySet<string> = new Set(PREVIEW_PAPERS.map((paper) => paper.id))
const LIBRARY_VIEWS = new Set<LibraryView>(["all", "unread", "saved"])
const READER_TABS = new Set<ReaderTab>(["overview", "notes", "details"])

function toSearchParams(search: string): URLSearchParams {
  return new URLSearchParams(search.startsWith("?") ? search.slice(1) : search)
}

function isResolvedTheme(value: string | null): value is ResolvedTheme {
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
  const params = toSearchParams(search)
  const paperId = params.get("paper")
  const view = params.get("view")
  const tab = params.get("tab")

  return {
    paperId: paperId !== null && PAPER_IDS.has(paperId) ? paperId : DEFAULT_PREVIEW_STATE.paperId,
    view: isLibraryView(view) ? view : DEFAULT_PREVIEW_STATE.view,
    tab: isReaderTab(tab) ? tab : DEFAULT_PREVIEW_STATE.tab,
    focus: parseBoolean(params.get("focus")),
    assistant: parseBoolean(params.get("assistant")),
  }
}

/**
 * Reads the optional `?theme=` request. The preview does not own a theme: a valid
 * request is applied once as the app-wide appearance preference and is not part of
 * the canonical preview URL.
 */
export function parseRequestedTheme(search: string): ResolvedTheme | null {
  const theme = toSearchParams(search).get("theme")
  return isResolvedTheme(theme) ? theme : null
}

export function createPreviewHref(state: PreviewState): string {
  const params = new URLSearchParams({
    paper: PAPER_IDS.has(state.paperId) ? state.paperId : DEFAULT_PREVIEW_STATE.paperId,
    view: state.view,
    tab: state.tab,
    focus: String(state.focus),
    assistant: String(state.assistant),
  })

  return `/paper-agent-lab?${params.toString()}`
}
