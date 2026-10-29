"use client"

import { useEffect, useRef } from "react"

export type RendererLifecycleState = {
  revision: number
  dirty: boolean
  activeTaskCount: number
  summary?: string
}

export type RendererQuitReason = "window-close" | "app-quit" | "server-change" | "local-stop"

type QuitRequest = {
  requestId: string
  reason: RendererQuitReason
  state: RendererLifecycleState
}

type QuitResponse = {
  requestId: string
  decision: "allow" | "cancel"
  state: RendererLifecycleState
}

type DesktopLifecycleBridge = {
  updateState(state: RendererLifecycleState): Promise<{ accepted: boolean; revision: number }>
  onQuitRequested(listener: (request: QuitRequest) => void): () => void
  respondToQuit(response: QuitResponse): Promise<{ accepted: boolean }>
}

type LifecycleWindow = Window & { agoraDesktop?: { version: 1; lifecycle: DesktopLifecycleBridge } }

// The workspace runs on the server: leaving the page drops what only this page holds.
const LEAVE_IMPACT = "离开后，未保存的草稿会丢失，进行中的请求会被中断且不会自动重放。"
const LEAVE_QUESTION: Record<RendererQuitReason, string> = {
  "window-close": "仍要关闭吗？",
  "app-quit": "仍要退出吗？",
  "server-change": "仍要切换连接方式吗？",
  "local-stop": "仍要停止本机服务吗？",
}

export function quitDecision(
  state: RendererLifecycleState,
  confirmAction: (message: string) => boolean,
  reason: RendererQuitReason = "app-quit",
): "allow" | "cancel" {
  if (!state.dirty && state.activeTaskCount === 0) return "allow"
  const parts = [state.dirty ? "仍有未保存草稿" : "", state.activeTaskCount > 0 ? `仍有 ${state.activeTaskCount} 个活动请求` : ""].filter(Boolean)
  return confirmAction(`${parts.join("，")}。${LEAVE_IMPACT}${LEAVE_QUESTION[reason] ?? LEAVE_QUESTION["app-quit"]}`) ? "allow" : "cancel"
}

export function useDesktopLifecycle(input: { dirty: boolean; activeTaskCount: number; summary?: string }) {
  const revisionRef = useRef(0)
  const stateRef = useRef<RendererLifecycleState>({ revision: 0, dirty: false, activeTaskCount: 0 })
  const acknowledgedRef = useRef(false)

  useEffect(() => {
    const bridge = (window as LifecycleWindow).agoraDesktop?.lifecycle
    if (!bridge) return
    const state: RendererLifecycleState = {
      revision: revisionRef.current + 1,
      dirty: input.dirty,
      activeTaskCount: Math.max(0, Math.floor(input.activeTaskCount)),
      ...(input.summary ? { summary: input.summary } : {}),
    }
    revisionRef.current = state.revision
    stateRef.current = state
    acknowledgedRef.current = false
    void bridge.updateState(state).then(result => {
      if (stateRef.current.revision !== state.revision) return
      acknowledgedRef.current = result.accepted && result.revision >= state.revision
      if (acknowledgedRef.current) return
      const retry = { ...stateRef.current, revision: revisionRef.current + 1, summary: stateRef.current.summary ? `${stateRef.current.summary}；宿主状态待确认` : "宿主状态待确认" }
      revisionRef.current = retry.revision
      stateRef.current = retry
      void bridge.updateState(retry).then(next => {
        if (stateRef.current.revision === retry.revision) acknowledgedRef.current = next.accepted && next.revision >= retry.revision
      }).catch(() => { acknowledgedRef.current = false })
    }).catch(() => { acknowledgedRef.current = false })
  }, [input.activeTaskCount, input.dirty, input.summary])

  useEffect(() => {
    const bridge = (window as LifecycleWindow).agoraDesktop?.lifecycle
    if (!bridge) return
    return bridge.onQuitRequested(request => {
      const state = stateRef.current
      const decision = quitDecision(state, message => window.confirm(message), request.reason)
      void bridge.respondToQuit({ requestId: request.requestId, decision, state }).catch(() => { /* Host owns timeout/fallback. */ })
    })
  }, [])
}
