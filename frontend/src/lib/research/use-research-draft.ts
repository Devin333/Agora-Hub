"use client"

import { useEffect, useRef, useState } from "react"
import { isResearchMode, type ResearchMode } from "./entry"
import { saveComposerDraft } from "./history"
import { useOwnedResearchWorkspace } from "./use-research-history"
import type { ResearchComposerDraft, ResearchConstraints } from "./workspace-items"

const legacyKey = "agora-home-draft:v1"
const emptyDraft = (): ResearchComposerDraft => ({ question: "", mode: "auto", constraints: {}, groupId: null, updatedAt: Date.now() })

export function useResearchDraft() {
  const workspace = useOwnedResearchWorkspace()
  const [local, setLocal] = useState<{ owner: string | null | undefined; draft: ResearchComposerDraft; initialized: boolean }>({ owner: undefined, draft: emptyDraft(), initialized: false })
  const [restored, setRestored] = useState(false)
  const current = useRef(local)
  current.current = local
  useEffect(() => {
    if (!workspace.ready || (current.current.initialized && current.current.owner === workspace.owner)) return
    let draft = workspace.workspace.composerDraft ?? emptyDraft()
    const params = new URLSearchParams(typeof window === "undefined" ? "" : window.location.search)
    const requestedGroup = params.get("researchGroup")
    if (requestedGroup && workspace.workspace.groups.some(group => group.id === requestedGroup)) draft = { ...draft, groupId: requestedGroup }
    if (!workspace.workspace.composerDraft && workspace.owner === null) {
      try {
        const saved = JSON.parse(sessionStorage.getItem(legacyKey) ?? "null")
        if (typeof saved?.query === "string" && saved.query.length <= 2000 && isResearchMode(saved.mode)) {
          draft = { ...draft, question: saved.query, mode: saved.mode }
          if (saveComposerDraft(draft, null)) sessionStorage.removeItem(legacyKey)
        }
      } catch { /* A legacy draft never crosses into a signed-in account. */ }
    }
    const materialIds = params.has("material") ? [...new Set(params.getAll("material"))] : draft.materialIds ?? []
    draft = { ...draft, materialIds: materialIds.filter(id => workspace.workspace.materials?.some(m => m.id === id)).slice(0, 50) }
    const next = { owner: workspace.owner, draft, initialized: true }
    current.current = next
    setLocal(next); setRestored(Boolean(draft.question))
  }, [workspace.ready, workspace.owner, workspace.workspace.composerDraft, workspace.workspace.groups, workspace.workspace.materials])

  const ready = workspace.ready && local.initialized && local.owner === workspace.owner
  const draft = ready ? { ...local.draft, groupId: workspace.workspace.groups.some(g => g.id === local.draft.groupId) ? local.draft.groupId : null,
    materialIds: (local.draft.materialIds ?? []).filter(id => workspace.workspace.materials?.some(m => m.id === id)),
  } : emptyDraft()
  function update(patch: Partial<ResearchComposerDraft>) {
    if (!ready) return
    // Multiple composer controls may update in one event before React renders again.
    // Read the latest draft rather than overwriting the preceding update with this render's copy.
    const next = { ...current.current.draft, ...patch, submittedSessionId: patch.submittedSessionId, updatedAt: Date.now() }
    current.current = { ...current.current, draft: next }
    setLocal(current.current); setRestored(false)
    return saveComposerDraft(next, local.owner)
  }
  function clear() {
    if (!ready) return
    current.current = { ...local, draft: emptyDraft() }
    setLocal(current.current); setRestored(false)
    saveComposerDraft(null, local.owner)
  }
  function markSubmitted(sessionId: string) { return update({ submittedSessionId: sessionId }) }
  return {
    query: draft.question, setQuery: (question: string) => update({ question }),
    mode: draft.mode, setMode: (mode: ResearchMode) => update({ mode, constraints: {} }),
    constraints: draft.constraints, setConstraints: (constraints: ResearchConstraints) => update({ constraints }),
    groupId: draft.groupId, setGroupId: (groupId: string | null) => update({ groupId }),
    materialIds: draft.materialIds ?? [], setMaterialIds: (materialIds: string[]) => update({ materialIds }),
    ready, restored, clear, markSubmitted, continueEditing: () => setRestored(false), update,
  }
}
