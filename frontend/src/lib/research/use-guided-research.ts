"use client"

import { useEffect, useRef, useState } from "react"
import { useSearchParams } from "next/navigation"
import { historyState, saveResearchConversation, updateResearchVisit, type ResearchVisit } from "./history"
import { useOwnedResearchWorkspace } from "./use-research-history"
import { conversationHref, researchIntentSchema, researchSearchResponseSchema, transitionResearchTurn, type ResearchConversation, type ResearchIntent, type ResearchSource, type ResearchTurn } from "./conversation"
import type { ResearchConstraints } from "./workspace-items"
import { guidedMaterialContext, guidedPreviousContext, waitForGuidedMaterials } from "./guided-context"

type ActiveResearch = { id: string; owner: string | null; groupId: string | null; conversation: ResearchConversation }
const running = (turn?: ResearchTurn) => turn?.phase === "understanding" || turn?.phase === "searching"

export function useGuidedResearch() {
  const workspace = useOwnedResearchWorkspace()
  const requestedSession = useSearchParams().get("researchSession")
  const [active, setActive] = useState<ActiveResearch | null>(null)
  const [saveError, setSaveError] = useState("")
  const state = useRef<ActiveResearch | null>(null)
  const generation = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const activeId = active?.id, activeOwner = active?.owner
  const activeDeleted = Boolean(workspace.workspace.visits.find(visit => visit.id === activeId)?.deletedAt)

  function persist(next: ActiveResearch) {
    if (historyState().owner !== next.owner) return false
    const saved = saveResearchConversation(next.id, next.conversation, next.groupId, next.owner)
    const message = historyState().message || "这次修改尚未保存，请重试。"
    if (!saved) {
      generation.current++; controller.current?.abort(); controller.current = null
      next = { ...next, conversation: { ...next.conversation, turns: next.conversation.turns.map(turn => running(turn) ? transitionResearchTurn(turn, "error", { error: message }) : turn) } }
    }
    state.current = next; setActive(next)
    setSaveError(saved ? "" : message)
    return saved
  }
  function patchTurn(id: string, update: (turn: ResearchTurn) => ResearchTurn) {
    const current = state.current
    if (!current || current.owner !== historyState().owner || historyState().workspace.visits.find(visit => visit.id === current.id)?.deletedAt) return
    return persist({ ...current, conversation: { ...current.conversation, turns: current.conversation.turns.map(turn => turn.id === id ? update(turn) : turn) } })
  }
  function stop() {
    generation.current++; controller.current?.abort(); controller.current = null
    const current = state.current, turn = current?.conversation.turns.at(-1)
    if (turn && running(turn)) patchTurn(turn.id, value => transitionResearchTurn(value, "stopped"))
  }
  function reset() { stop(); state.current = null; setActive(null); setSaveError(""); window.history.replaceState(null, "", "/design-demo") }
  function resume(visit: ResearchVisit) {
    if (!visit.conversation || visit.deletedAt || !workspace.ready) return false
    stop()
    const conversation = { ...visit.conversation, turns: visit.conversation.turns.map(turn => running(turn) ? transitionResearchTurn(turn, "stopped") : turn) }
    persist({ id: visit.id, owner: workspace.owner!, groupId: visit.groupId, conversation })
    window.history.replaceState(null, "", conversationHref(visit.id))
    return true
  }
  useEffect(() => {
    if (active && (!workspace.ready || active.owner !== workspace.owner || activeDeleted)) {
      generation.current++; controller.current?.abort(); state.current = null; setActive(null); setSaveError("")
    }
    if (!workspace.ready || !requestedSession || (state.current?.id === requestedSession && state.current.owner === workspace.owner)) return
    const visit = workspace.workspace.visits.find(item => item.id === requestedSession && !item.deletedAt)
    if (visit?.conversation) resume(visit)
    // Restore only on navigation or owner changes; never replay the active request after a save.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace.ready, workspace.owner, requestedSession, activeDeleted])
  useEffect(() => () => { generation.current++; controller.current?.abort() }, [])
  useEffect(() => {
    if (!activeId) return
    const element = document.querySelector<HTMLElement>('[aria-label="研究工作区"]')
    if (!element) return
    const visit = historyState().workspace.visits.find(item => item.id === activeId)
    element.scrollTop = visit?.scrollY ?? 0
    let timer: ReturnType<typeof setTimeout> | undefined
    const savePosition = () => {
      const current = state.current
      if (current?.id === activeId && historyState().owner === activeOwner) saveResearchConversation(current.id, current.conversation, current.groupId, current.owner, element.scrollTop)
    }
    const onScroll = () => { if (timer) clearTimeout(timer); timer = setTimeout(savePosition, 250) }
    element.addEventListener("scroll", onScroll); window.addEventListener("pagehide", savePosition)
    return () => { if (timer) clearTimeout(timer); element.removeEventListener("scroll", onScroll); window.removeEventListener("pagehide", savePosition); savePosition() }
  }, [activeId, activeOwner])

  function beginRequest() {
    controller.current?.abort()
    const request = ++generation.current, owner = state.current?.owner
    const abort = new AbortController(); controller.current = abort
    const isLatest = () => request === generation.current && owner === historyState().owner
    const isCurrent = () => isLatest() && !abort.signal.aborted
    return { abort, isCurrent, isLatest }
  }
  async function request(path: string, body: unknown, signal: AbortSignal) {
    const response = await fetch(path, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal })
    const envelope = await response.json()
    if (!response.ok || !envelope.success) throw new Error(envelope?.error?.message || "研究服务暂时不可用，请稍后重试。")
    return envelope.data
  }
  async function understand(turnId: string, skip = false) {
    const current = state.current, turn = current?.conversation.turns.find(item => item.id === turnId)
    if (!current || !turn) return
    if (turn.events.filter(event => event.phase === "understanding").length >= 8) { setSaveError("本轮尝试次数较多，请新建研究继续。已有内容已保留。"); return }
    if (!patchTurn(turnId, value => transitionResearchTurn(value, "understanding", { error: undefined }))) return
    const { abort, isCurrent, isLatest } = beginRequest()
    const timer = setTimeout(() => abort.abort("timeout"), 65000)
    try {
      const materialIds = turn.materialIds ?? current.conversation.materialIds
      await waitForGuidedMaterials(current.owner, materialIds, abort.signal)
      const payload = await request("/api/research/intent", { question: turn.question, answers: turn.answers, previous: guidedPreviousContext(current.conversation), ...guidedMaterialContext(current.owner, materialIds), skipClarification: skip || turn.answers.length >= 2, requestedSources: turn.requestedSources, constraints: turn.requestedConstraints }, abort.signal)
      if (!isCurrent()) return
      const intent = researchIntentSchema.parse(payload)
      if (intent.clarification && !skip && turn.answers.length < 2) patchTurn(turnId, value => transitionResearchTurn(value, "clarifying", { intent }))
      else {
        patchTurn(turnId, value => transitionResearchTurn(value, "confirming", { intent: { ...intent, clarification: null } }))
        if (current.conversation.mode === "auto" && !intent.confirmationRequired) await search(turnId)
      }
    } catch (error) {
      if (!isLatest()) return
      if (abort.signal.reason === "timeout") patchTurn(turnId, value => transitionResearchTurn(value, "error", { error: "理解问题的时间较长，请重试或修改问题。" }))
      else if (isCurrent()) patchTurn(turnId, value => transitionResearchTurn(value, "error", { error: error instanceof Error && error.name !== "ZodError" ? error.message.slice(0, 500) : "返回的问题信息不完整，请重试。" }))
    } finally { clearTimeout(timer) }
  }
  async function search(turnId: string, only?: ResearchSource[]) {
    const current = state.current, turn = current?.conversation.turns.find(item => item.id === turnId)
    if (!current || !turn?.intent || running(turn)) return
    if (turn.events.filter(event => event.phase === "searching").length >= 5) { setSaveError("本轮查找已尝试 5 次，请稍后新建研究继续。已有结果已保留。"); return }
    const sources = only ?? turn.intent.sources
    const intent = turn.intent
    if (!patchTurn(turnId, value => transitionResearchTurn(value, "searching", { error: undefined, searches: value.searches.filter(item => !sources.includes(item.source)), failures: value.failures.filter(item => !sources.includes(item.source)) }))) return
    const { abort, isCurrent, isLatest } = beginRequest()
    const timer = setTimeout(() => abort.abort("timeout"), 65000)
    await Promise.allSettled(sources.map(async source => {
      try {
        const materialIds = turn.materialIds ?? current.conversation.materialIds
        await waitForGuidedMaterials(current.owner, materialIds, abort.signal)
        const payload = await request("/api/research/search", { source, intent, ...guidedMaterialContext(current.owner, materialIds) }, abort.signal)
        if (!isCurrent()) return
        const result = researchSearchResponseSchema.parse(payload)
        if (result.source !== source) throw new Error("返回的资料类型不匹配，请重试。")
        patchTurn(turnId, value => ({ ...value, searches: [...value.searches.filter(item => item.source !== source), result] }))
      } catch (error) {
        if (isLatest() && (!abort.signal.aborted || abort.signal.reason === "timeout")) patchTurn(turnId, value => ({ ...value, failures: [...value.failures.filter(item => item.source !== source), { source, message: abort.signal.reason === "timeout" ? "查找时间较长，请重试。" : error instanceof Error && error.name !== "ZodError" ? error.message.slice(0, 500) : "返回的资料不完整，请重试。" }] }))
      }
    }))
    clearTimeout(timer)
    if (isLatest() && (!abort.signal.aborted || abort.signal.reason === "timeout")) patchTurn(turnId, value => transitionResearchTurn(value, "results"))
  }
  function start(question: string, mode: "auto" | "plan", groupId: string | null, materialIds: string[], constraints: ResearchConstraints = {}, requestedSources?: ResearchSource[]) {
    if (!workspace.ready || !question.trim()) return
    stop()
    const now = Date.now(), turn: ResearchTurn = { id: crypto.randomUUID(), question: question.trim(), materialIds: [...materialIds], answers: [], phase: "understanding", requestedConstraints: constraints, ...(requestedSources ? { requestedSources } : {}), searches: [], failures: [], events: [{ phase: "understanding", at: now }], createdAt: now }
    const next: ActiveResearch = { id: crypto.randomUUID(), owner: workspace.owner!, groupId, conversation: { version: 1, mode, materialIds, draft: "", turns: [turn] } }
    if (!persist(next)) return
    window.history.replaceState(null, "", conversationHref(next.id))
    void understand(turn.id)
  }
  function followUp(question: string) {
    const current = state.current, last = current?.conversation.turns.at(-1)
    if (!current || !last || running(last) || !question.trim()) return
    if (last.phase === "clarifying" && last.answers.length < 2) {
      if (persist({ ...current, conversation: { ...current.conversation, draft: "", turns: current.conversation.turns.map(turn => turn.id === last.id ? { ...turn, answers: [...turn.answers, question.trim()] } : turn) } })) void understand(last.id)
      return
    }
    if (current.conversation.turns.length >= 20) { setSaveError("这次研究已有 20 轮，请新建研究继续。已有内容已保留。"); return }
    const now = Date.now(), turn: ResearchTurn = { id: crypto.randomUUID(), question: question.trim(), materialIds: [...current.conversation.materialIds], answers: [], phase: "understanding", searches: [], failures: [], events: [{ phase: "understanding", at: now }], createdAt: now }
    if (persist({ ...current, conversation: { ...current.conversation, draft: "", turns: [...current.conversation.turns, turn] } })) void understand(turn.id)
  }
  function editIntent(patch: Partial<ResearchIntent>) {
    const turn = state.current?.conversation.turns.at(-1)
    if (turn?.intent && !running(turn)) patchTurn(turn.id, value => ({ ...value, intent: { ...value.intent!, ...patch } }))
  }
  const visible = active?.owner === workspace.owner && workspace.ready && !activeDeleted ? active : null
  const last = visible?.conversation.turns.at(-1)
  return { active: visible, busy: running(last), saveError, start, followUp, reset, resume, stop, editIntent,
    setMaterialIds: (materialIds: string[]) => { const current = state.current; if (current && !running(current.conversation.turns.at(-1))) persist({ ...current, conversation: { ...current.conversation, materialIds } }) },
    setGroupId: (groupId: string | null) => { const current = state.current; if (current && updateResearchVisit(current.id, { groupId })) persist({ ...current, groupId }) },
    setDraft: (draft: string) => { const current = state.current; if (current) persist({ ...current, conversation: { ...current.conversation, draft } }) },
    confirm: () => { if (last) void search(last.id) },
    skip: () => { if (last) void understand(last.id, true) },
    retry: (source?: ResearchSource) => { if (last) { if (last.intent && last.phase !== "error" && (last.phase !== "stopped" || last.events.at(-2)?.phase === "searching")) void search(last.id, source ? [source] : last.intent.sources); else void understand(last.id) } },
  }
}
