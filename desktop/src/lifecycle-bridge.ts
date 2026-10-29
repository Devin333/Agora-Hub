import { randomUUID } from "node:crypto"
import type { BrowserWindow } from "electron"
import { HOST_EVENTS } from "./channels"
import type { QuitRequest, QuitResponse, RendererLifecycleState } from "./contracts"

const DEFAULT_STATE: RendererLifecycleState = { revision: 0, dirty: false, activeTaskCount: 0 }

export type RendererQuitUnknownReason = "timeout" | "invalid-response" | "renderer-gone" | "send-failed" | "request-in-progress" | "renderer-unavailable"

export type RendererQuitAssessment =
  | { status: "confirmed"; decision: "allow" | "cancel"; state: RendererLifecycleState }
  | { status: "unknown"; reason: RendererQuitUnknownReason; lastKnownState: RendererLifecycleState }

interface PendingQuit {
  request: QuitRequest
  resolve: (assessment: RendererQuitAssessment) => void
  timer: NodeJS.Timeout
}

export function validateRendererState(value: unknown, previousRevision = -1): RendererLifecycleState | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null
  const state = value as Record<string, unknown>
  if (!Number.isSafeInteger(state.revision) || Number(state.revision) < 0 || Number(state.revision) <= previousRevision) return null
  if (typeof state.dirty !== "boolean" || !Number.isSafeInteger(state.activeTaskCount) || Number(state.activeTaskCount) < 0 || Number(state.activeTaskCount) > 1000) return null
  if (state.summary !== undefined && (typeof state.summary !== "string" || state.summary.length > 240 || /[\r\n\0]/.test(state.summary))) return null
  return {
    revision: Number(state.revision),
    dirty: state.dirty,
    activeTaskCount: Number(state.activeTaskCount),
    ...(typeof state.summary === "string" ? { summary: state.summary } : {})
  }
}

function sameState(left: RendererLifecycleState, right: RendererLifecycleState): boolean {
  return left.revision === right.revision && left.dirty === right.dirty && left.activeTaskCount === right.activeTaskCount && left.summary === right.summary
}

export function requiresNativeQuitConfirmation(assessment: RendererQuitAssessment): boolean {
  return assessment.status === "unknown" || assessment.state.dirty || assessment.state.activeTaskCount > 0
}

export class RendererLifecycleBridge {
  private state: RendererLifecycleState = { ...DEFAULT_STATE }
  private pending: PendingQuit | null = null

  update(value: unknown): { accepted: boolean; revision: number } {
    const next = validateRendererState(value, this.state.revision)
    if (!next) return { accepted: false, revision: this.state.revision }
    this.state = next
    return { accepted: true, revision: next.revision }
  }

  snapshot(): RendererLifecycleState {
    return { ...this.state }
  }

  needsConfirmation(): boolean {
    return this.state.dirty || this.state.activeTaskCount > 0
  }

  async requestQuit(window: BrowserWindow, reason: QuitRequest["reason"], timeoutMs = 3000): Promise<RendererQuitAssessment> {
    if (this.pending) return { status: "unknown", reason: "request-in-progress", lastKnownState: this.snapshot() }
    if (window.isDestroyed() || window.webContents.isDestroyed()) {
      return { status: "unknown", reason: "renderer-unavailable", lastKnownState: this.snapshot() }
    }

    const request: QuitRequest = { requestId: randomUUID(), reason, state: this.snapshot() }
    return await new Promise<RendererQuitAssessment>(resolve => {
      const timer = setTimeout(() => this.resolveUnknown(request.requestId, "timeout"), timeoutMs)
      this.pending = { request, resolve, timer }
      try {
        window.webContents.send(HOST_EVENTS.lifecycleQuitRequested, request)
      } catch {
        this.resolveUnknown(request.requestId, "send-failed")
      }
    })
  }

  respond(value: unknown): { accepted: boolean } {
    if (!this.pending) return { accepted: false }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      this.resolveUnknown(this.pending.request.requestId, "invalid-response")
      return { accepted: false }
    }

    const response = value as Partial<QuitResponse>
    const responseState = validateRendererState(response.state, this.state.revision - 1)
    if (response.requestId !== this.pending.request.requestId || (response.decision !== "allow" && response.decision !== "cancel") || !responseState || !sameState(responseState, this.state)) {
      this.resolveUnknown(this.pending.request.requestId, "invalid-response")
      return { accepted: false }
    }

    const pending = this.pending
    clearTimeout(pending.timer)
    this.pending = null
    pending.resolve({ status: "confirmed", decision: response.decision, state: this.snapshot() })
    return { accepted: true }
  }

  rendererGone(): void {
    if (this.pending) this.resolveUnknown(this.pending.request.requestId, "renderer-gone")
  }

  /**
   * A new document replaced the page (reload, server redirect, another server):
   * its revisions start over and nothing the old document reported still holds.
   */
  documentReplaced(): void {
    this.rendererGone()
    this.state = { ...DEFAULT_STATE }
  }

  cancelPending(): void {
    if (!this.pending) return
    const pending = this.pending
    clearTimeout(pending.timer)
    this.pending = null
    pending.resolve({ status: "confirmed", decision: "cancel", state: this.snapshot() })
  }

  private resolveUnknown(requestId: string, reason: RendererQuitUnknownReason): void {
    if (!this.pending || this.pending.request.requestId !== requestId) return
    const pending = this.pending
    clearTimeout(pending.timer)
    this.pending = null
    pending.resolve({ status: "unknown", reason, lastKnownState: this.snapshot() })
  }
}
