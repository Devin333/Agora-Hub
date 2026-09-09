import { historyState } from "./history"
import { retryHistoryEvent } from "./history-sync"
import type { ResearchConversation } from "./conversation"

/** Public references are untrusted input. Private PDF references always resolve on the server. */
export function guidedMaterialContext(owner: string | null, identifiers: string[]) {
  const state = historyState()
  if (state.owner !== owner) throw new Error("账号已切换，请重新打开这次研究。")
  const materials = identifiers.map(id => state.workspace.materials?.find(material => material.id === id))
  if (materials.some(material => !material)) throw new Error("部分资料已被移除，请重新选择。")
  if (owner) return { materialIds: identifiers }
  if (materials.some(material => material!.kind === "pdf" || (material!.readerHref && !material!.url))) throw new Error("请登录后使用私有论文。")
  return { materialIds: [], publicSources: materials.map(material => ({ id: material!.id, kind: material!.kind, title: material!.title, url: material!.url, notes: material!.notes?.slice(0, 4000) })) }
}

/** Attachments must reach owned storage before their IDs are used by the research API. */
export async function waitForGuidedMaterials(owner: string | null, identifiers: string[], signal: AbortSignal) {
  if (!owner || !identifiers.length) return
  const startedAt = Date.now()
  window.dispatchEvent(new Event(retryHistoryEvent))
  while (true) {
    signal.throwIfAborted()
    const state = historyState()
    if (state.owner !== owner) throw new Error("账号已切换，请重新打开这次研究。")
    if (state.status === "conflict" || state.status === "error") throw new Error("资料尚未同步，请先在左侧重试同步。")
    if (!state.dirty) return
    if (Date.now() - startedAt > 12000) throw new Error("资料同步较慢，请稍后重试。问题和资料已保留。")
    await new Promise<void>((resolve, reject) => {
      const cancel = () => { clearTimeout(timer); signal.removeEventListener("abort", cancel); reject(signal.reason) }
      const timer = setTimeout(() => { signal.removeEventListener("abort", cancel); resolve() }, 100)
      signal.addEventListener("abort", cancel, { once: true })
    })
  }
}

/** Send bounded prior context without duplicating long descriptions or event transcripts. */
export function guidedPreviousContext(conversation: ResearchConversation) {
  return conversation.turns.slice(0, -1).map((turn, index, turns) => ({
    ...turn,
    searches: index === turns.length - 1 ? turn.searches.map(search => ({ ...search, results: search.results.map(result => ({ ...result, description: "" })) })) : [],
    events: [turn.events[0], ...(turn.events.length > 1 ? [turn.events[turn.events.length - 1]] : [])],
  }))
}
