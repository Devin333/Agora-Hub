"use client"

import { useEffect, useState } from "react"
import { AccountServiceError } from "@/lib/account/client"
import {
  previewModelConnection,
  type ModelConnectionInput,
  type ModelConnectionPreview,
} from "@/lib/account/model-connections"

// Typing pauses this long before the draft is sent, so a burst of keystrokes
// becomes one request.
export const PREVIEW_DEBOUNCE_MS = 300

export type PurposeFitPreviewState =
  | { kind: "idle" }
  | { kind: "loading"; last: ModelConnectionPreview | null }
  | { kind: "ready"; preview: ModelConnectionPreview }
  // The draft is not complete enough to judge; `field` names what is missing.
  | { kind: "incomplete"; field: string | null; message: string }
  | { kind: "failed"; message: string }

/**
 * The API's answer to "which purposes would this draft serve".
 *
 * The draft goes through the same validation and admission as saving it, so
 * the form never keeps its own copy of the window arithmetic. The request
 * carries settings only; the key is never sent while typing.
 */
export function usePurposeFitPreview(input: ModelConnectionInput | null): PurposeFitPreviewState {
  const [state, setState] = useState<PurposeFitPreviewState>({ kind: "idle" })
  // Serialised so a re-render with an equal draft does not ask again.
  const key = input ? JSON.stringify({ ...input, apiKey: undefined }) : null

  useEffect(() => {
    if (key === null || input === null) { setState({ kind: "idle" }); return }
    const controller = new AbortController()
    setState(previous => ({ kind: "loading", last: previous.kind === "ready" ? previous.preview : previous.kind === "loading" ? previous.last : null }))
    const timer = window.setTimeout(() => {
      previewModelConnection(input, controller.signal)
        .then(preview => { if (!controller.signal.aborted) setState({ kind: "ready", preview }) })
        .catch(cause => {
          if (controller.signal.aborted) return
          if (cause instanceof AccountServiceError && cause.code === "model_connection_invalid") {
            const field = typeof cause.details.field === "string" ? cause.details.field : null
            setState({ kind: "incomplete", field, message: cause.message })
            return
          }
          setState({ kind: "failed", message: cause instanceof AccountServiceError ? cause.message : "暂时无法判断这个模型能用于哪些用途。" })
        })
    }, PREVIEW_DEBOUNCE_MS)
    return () => { window.clearTimeout(timer); controller.abort() }
    // `key` stands for `input`; the object identity changes on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  return state
}
