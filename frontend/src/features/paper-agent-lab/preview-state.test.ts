import { describe, expect, it } from "vitest"
import { PREVIEW_PAPERS } from "./data"
import {
  DEFAULT_PREVIEW_STATE,
  createPreviewHref,
  parsePreviewState,
  parseRequestedTheme,
} from "./preview-state"
import type { PreviewState } from "./types"

const paperIds = [
  "memgpt",
  "generative-agents",
  "rag",
  "lost-middle",
  "react",
  "attention",
] as const

describe("paper agent preview state", () => {
  it("keeps the fixed paper catalogue and desktop-workspace default stable", () => {
    expect(PREVIEW_PAPERS.map((paper) => paper.id)).toEqual(paperIds)
    expect(DEFAULT_PREVIEW_STATE).toEqual({
      paperId: "memgpt",
      view: "all",
      tab: "overview",
      focus: false,
      assistant: false,
    })
  })

  it.each(paperIds)("accepts the supported %s paper and every canonical field", (paperId) => {
    expect(parsePreviewState(
      `?theme=dark&paper=${paperId}&view=saved&tab=details&focus=true&assistant=true`,
    )).toEqual({
      paperId,
      view: "saved",
      tab: "details",
      focus: true,
      assistant: true,
    })
  })

  it.each([
    ["", DEFAULT_PREVIEW_STATE],
    ["?theme=sepia&paper=missing&view=recent&tab=full&focus=1&assistant=yes", DEFAULT_PREVIEW_STATE],
    ["theme=dark&paper=rag&view=unread&tab=notes&focus=true&assistant=true", {
      paperId: "rag",
      view: "unread",
      tab: "notes",
      focus: true,
      assistant: true,
    }],
    ["?theme=dark&paper=missing&view=saved&tab=invalid&focus=false&assistant=false", {
      paperId: "memgpt",
      view: "saved",
      tab: "overview",
      focus: false,
      assistant: false,
    }],
    ["?style=gallery&mode=app&theme=dark", DEFAULT_PREVIEW_STATE],
  ] as const)("normalizes %s without throwing", (search, expected) => {
    expect(parsePreviewState(search)).toEqual(expected)
  })

  it("uses the first value for duplicate canonical parameters and rejects non-canonical booleans", () => {
    expect(parsePreviewState(
      "?paper=rag&paper=memgpt&view=unread&view=all&tab=notes&focus=TRUE&assistant=1",
    )).toEqual({
      paperId: "rag",
      view: "unread",
      tab: "notes",
      focus: false,
      assistant: false,
    })
  })

  it.each([
    ["?theme=dark", "dark"],
    ["theme=light&paper=rag", "light"],
    ["?theme=dark&theme=light", "dark"],
    ["?theme=sepia", null],
    ["?theme=system", null],
    ["?theme=DARK", null],
    ["", null],
  ] as const)("reads the one-off appearance request in %s as %s", (search, expected) => {
    expect(parseRequestedTheme(search)).toBe(expected)
  })

  it("serializes the complete state in one stable canonical order without a theme", () => {
    const state: PreviewState = {
      paperId: "lost-middle",
      view: "unread",
      tab: "notes",
      focus: true,
      assistant: true,
    }

    expect(createPreviewHref(state)).toBe(
      "/paper-agent-lab?paper=lost-middle&view=unread&tab=notes&focus=true&assistant=true",
    )
    expect(parsePreviewState(createPreviewHref(state).split("?")[1])).toEqual(state)
  })

  it("falls back only an unknown paper while preserving the remaining serialized state", () => {
    expect(createPreviewHref({
      paperId: "unknown",
      view: "saved",
      tab: "details",
      focus: false,
      assistant: true,
    })).toBe(
      "/paper-agent-lab?paper=memgpt&view=saved&tab=details&focus=false&assistant=true",
    )
  })
})
