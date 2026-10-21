import { describe, expect, it } from "vitest"
import { PREVIEW_PAPERS } from "./data"
import {
  DEFAULT_PREVIEW_STATE,
  createPreviewHref,
  parsePreviewState,
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
      theme: "light",
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
      theme: "dark",
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
      theme: "dark",
      paperId: "rag",
      view: "unread",
      tab: "notes",
      focus: true,
      assistant: true,
    }],
    ["?theme=dark&paper=missing&view=saved&tab=invalid&focus=false&assistant=false", {
      theme: "dark",
      paperId: "memgpt",
      view: "saved",
      tab: "overview",
      focus: false,
      assistant: false,
    }],
    ["?style=gallery&mode=app&theme=dark", {
      ...DEFAULT_PREVIEW_STATE,
      theme: "dark",
    }],
  ] as const)("normalizes %s without throwing", (search, expected) => {
    expect(parsePreviewState(search)).toEqual(expected)
  })

  it("uses the first value for duplicate canonical parameters and rejects non-canonical booleans", () => {
    expect(parsePreviewState(
      "?theme=dark&theme=light&paper=rag&paper=memgpt&view=unread&tab=notes&focus=TRUE&assistant=1",
    )).toEqual({
      theme: "dark",
      paperId: "rag",
      view: "unread",
      tab: "notes",
      focus: false,
      assistant: false,
    })
  })

  it("serializes the complete state in one stable canonical order", () => {
    const state: PreviewState = {
      theme: "dark",
      paperId: "lost-middle",
      view: "unread",
      tab: "notes",
      focus: true,
      assistant: true,
    }

    expect(createPreviewHref(state)).toBe(
      "/paper-agent-lab?theme=dark&paper=lost-middle&view=unread&tab=notes&focus=true&assistant=true",
    )
    expect(parsePreviewState(createPreviewHref(state).split("?")[1])).toEqual(state)
  })

  it("falls back only an unknown paper while preserving the remaining serialized state", () => {
    expect(createPreviewHref({
      theme: "dark",
      paperId: "unknown",
      view: "saved",
      tab: "details",
      focus: false,
      assistant: true,
    })).toBe(
      "/paper-agent-lab?theme=dark&paper=memgpt&view=saved&tab=details&focus=false&assistant=true",
    )
  })
})