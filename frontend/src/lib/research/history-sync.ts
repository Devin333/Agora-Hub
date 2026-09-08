import { acceptAccountHistory, acknowledgeAccountHistory, historyState, pendingHistoryKey, researchHistoryEvent, restorePendingHistory, selectHistoryOwner, setHistoryStatus } from "./history"
import { validateWorkspace, type ResearchSnapshot } from "./history-model"

export const retryHistoryEvent = "agora-research-history-retry"
export const reloadHistoryEvent = "agora-research-history-reload"
class HistoryRequestError extends Error { constructor(public status: number) { super("history request failed") } }
async function requestHistory(signal: AbortSignal, snapshot?: ResearchSnapshot): Promise<ResearchSnapshot> {
  const response = await fetch("/api/research/history", { method: snapshot ? "PUT" : "GET", signal, credentials: "same-origin", cache: "no-store", headers: { "Content-Type": "application/json" }, ...(snapshot ? { body: JSON.stringify(snapshot) } : {}) })
  const result = await response.json()
  if (!response.ok || !result.success) throw new HistoryRequestError(response.status)
  const value = result.data
  const workspace = validateWorkspace(value)
  if (!workspace || !Number.isSafeInteger(value.revision) || value.revision < 0) throw new Error("invalid history response")
  return { ...workspace, revision: value.revision }
}

/** One owner and one write in flight; stale async completions cannot reach a new account. */
export function startHistorySync(userId: string | null | undefined): () => void {
  selectHistoryOwner(userId)
  if (!userId) return () => {}
  const controller = new AbortController()
  let stopped = false, inFlight = false, timer: ReturnType<typeof setTimeout> | undefined
  const current = () => !stopped && historyState().owner === userId
  function fail(cause: unknown) {
    if (!current()) return
    const code = cause instanceof HistoryRequestError ? cause.status : 0
    setHistoryStatus(code === 409 ? "conflict" : "error", code === 409 ? "另一处已更新历史。本次修改已保留，可先导出备份，再加载最新记录。" : code === 401 ? "登录已过期，请重新登录后同步。" : "账号历史暂时无法同步。修改已保留，请重试。")
  }
  async function load(discard = false) {
    if (!current() || inFlight) return
    inFlight = true
    const before = historyState().generation
    try {
      const result = await requestHistory(controller.signal)
      if (!current() || (!discard && historyState().generation !== before)) return
      let pending: ResearchSnapshot | null = null
      try {
        const value = JSON.parse(sessionStorage.getItem(pendingHistoryKey(userId!)) ?? "null")
        if (!discard && validateWorkspace(value) && Number.isSafeInteger(value.revision)) pending = value
        if (discard) sessionStorage.removeItem(pendingHistoryKey(userId!))
      } catch { /* A malformed local draft never replaces server data. */ }
      acceptAccountHistory(result, result.revision)
      if (pending) {
        restorePendingHistory(pending, pending.revision)
        if (pending.revision !== result.revision) fail(new HistoryRequestError(409))
      }
    } catch (cause) { fail(cause) }
    finally { inFlight = false; schedule() }
  }
  async function save() {
    const state = historyState()
    if (!current() || inFlight || !state.dirty || state.status === "conflict") return
    inFlight = true
    try {
      const result = await requestHistory(controller.signal, { ...state.workspace, revision: state.revision })
      if (current()) acknowledgeAccountHistory(result.revision, state.generation)
    } catch (cause) { fail(cause) }
    finally { inFlight = false; schedule() }
  }
  function schedule() {
    if (timer) clearTimeout(timer)
    const state = historyState()
    if (current() && !inFlight && state.dirty && state.status === "saving") timer = setTimeout(save, 500)
  }
  const retry = () => { if (historyState().dirty) { setHistoryStatus("saving"); void save() } else void load() }
  const reload = () => void load(true)
  const focus = () => { const state = historyState(); if (!state.dirty) void load() }
  const beforeUnload = (event: BeforeUnloadEvent) => { if (historyState().dirty) { event.preventDefault(); event.returnValue = "" } }
  window.addEventListener(researchHistoryEvent, schedule)
  window.addEventListener(retryHistoryEvent, retry)
  window.addEventListener(reloadHistoryEvent, reload)
  window.addEventListener("focus", focus)
  window.addEventListener("beforeunload", beforeUnload)
  void load()
  return () => {
    stopped = true; controller.abort(); if (timer) clearTimeout(timer)
    window.removeEventListener(researchHistoryEvent, schedule); window.removeEventListener(retryHistoryEvent, retry)
    window.removeEventListener(reloadHistoryEvent, reload); window.removeEventListener("focus", focus); window.removeEventListener("beforeunload", beforeUnload)
  }
}
