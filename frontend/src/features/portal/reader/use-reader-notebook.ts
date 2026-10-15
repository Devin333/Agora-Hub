"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { isLocalDesktopProfile } from "@/lib/deployment-profile"
import { acceptAccountHistory, historyState, saveReaderNotebook } from "@/lib/research/history"
import { validateWorkspace, type ResearchWorkspace } from "@/lib/research/history-model"
import { retryHistoryEvent } from "@/lib/research/history-sync"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import type { ResearchReaderNotebook } from "@/lib/research/workspace-items"

type NotebookPatch = Partial<Pick<ResearchReaderNotebook, "note" | "question" | "fontSize" | "sectionId" | "pdfPage" | "readerView">>
type NotebookDraft = {
  data: ResearchReaderNotebook
  baseRevision: number
  generation: number
  baseWorkspace: ResearchWorkspace
  baseWasClean: boolean
  conflict: boolean
  error: string
}

// This is an unsaved-draft guard, never an official notebook store. Confirmed
// content and restart recovery still come exclusively from owned History.
const unsavedDrafts = new Map<string, NotebookDraft>()

function emptyNotebook(paperId: string, sourceRevision: string): ResearchReaderNotebook {
  return { paperId, sourceRevision, note: "", question: "", fontSize: 18, sectionId: "", pdfPage: 1, readerView: "text", updatedAt: Date.now() }
}

function restoreNotebook(value: ResearchReaderNotebook | undefined, paperId: string, sourceRevision: string): ResearchReaderNotebook {
  if (!value) return emptyNotebook(paperId, sourceRevision)
  const exact = Boolean(sourceRevision) && value.sourceRevision === sourceRevision
  return { ...value, paperId, sourceRevision, sectionId: exact ? value.sectionId : "", pdfPage: exact ? value.pdfPage : 1, readerView: exact && value.readerView === "pdf" ? "pdf" : "text" }
}

function validLocal(value: unknown, paperId: string, sourceRevision: string): ResearchReaderNotebook | null {
  if (!value || typeof value !== "object") return null
  const item = value as Partial<ResearchReaderNotebook>
  const exact = item.sourceRevision === sourceRevision
  return {
    paperId, sourceRevision, note: typeof item.note === "string" ? item.note : "",
    question: typeof item.question === "string" ? item.question.slice(0, 2000) : "",
    fontSize: [16, 18, 20, 22].includes(Number(item.fontSize)) ? item.fontSize as 16 | 18 | 20 | 22 : 18,
    sectionId: exact && typeof item.sectionId === "string" ? item.sectionId.slice(0, 200) : "",
    pdfPage: exact && Number.isSafeInteger(item.pdfPage) && Number(item.pdfPage) > 0 ? Number(item.pdfPage) : 1,
    readerView: exact && item.readerView === "pdf" ? "pdf" : "text",
    updatedAt: typeof item.updatedAt === "number" ? item.updatedAt : Date.now(),
  }
}

function sameNotebook(left: ResearchReaderNotebook | undefined, right: ResearchReaderNotebook): boolean {
  return Boolean(left && left.paperId === right.paperId && left.sourceRevision === right.sourceRevision && left.note === right.note && left.question === right.question && left.fontSize === right.fontSize && left.sectionId === right.sectionId && left.pdfPage === right.pdfPage && (left.readerView ?? "text") === (right.readerView ?? "text") && left.updatedAt === right.updatedAt)
}

function withoutNotebook(workspace: ResearchWorkspace, paperId: string): string {
  return JSON.stringify({ ...workspace, readerNotebooks: (workspace.readerNotebooks ?? []).filter(item => item.paperId !== paperId) })
}

export function useReaderNotebook(paperId: string, sourceRevision = "") {
  const workspace = useOwnedResearchWorkspace()
  const owner = workspace.owner
  const exactSource = Boolean(sourceRevision)
  const canPersist = !isLocalDesktopProfile() || exactSource
  const scope = JSON.stringify([owner, paperId, sourceRevision])
  const scopeRef = useRef(scope)
  scopeRef.current = scope
  const mounted = useRef(true)
  const resolvingRequest = useRef<AbortController | null>(null)
  const adoptedDraft = useRef<NotebookDraft | null>(null)
  const current = useRef(emptyNotebook(paperId, sourceRevision))
  const [data, setData] = useState(current.current)
  const [loadedScope, setLoadedScope] = useState("")
  const [storageError, setStorageError] = useState(false)
  const [saveError, setSaveError] = useState("")
  const [legacyAvailable, setLegacyAvailable] = useState(false)
  const [resolving, setResolving] = useState(false)
  const [, changed] = useState(0)
  const guestKey = `agora:reader-notebook:v2:${paperId}:${sourceRevision}`
  const legacyKey = `agora:reader-notebook:v1:${paperId}`
  const stored = useMemo(() => workspace.workspace.readerNotebooks?.find(item => item.paperId === paperId), [paperId, workspace.workspace.readerNotebooks])
  const revisionChanged = Boolean(exactSource && stored && stored.sourceRevision !== sourceRevision)
  const ready = workspace.ready && loadedScope === scope
  const inScope = useCallback(() => mounted.current && scopeRef.current === scope && historyState().owner === owner, [owner, scope])

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; resolvingRequest.current?.abort() }
  }, [])

  useEffect(() => {
    resolvingRequest.current?.abort()
    adoptedDraft.current = null
    setResolving(false)
  }, [scope])

  useEffect(() => {
    if (!workspace.ready) { setLoadedScope(""); return }
    let restored = restoreNotebook(stored, paperId, sourceRevision)
    let draft = owner ? unsavedDrafts.get(scope) : undefined
    // A module-held draft may survive a component unmount, but it must not cross
    // a later owner-workspace restore. The currently mounted instance may keep
    // protecting its own draft while an async receipt rotates generation.
    if (draft && draft.generation !== workspace.generation && adoptedDraft.current !== draft) {
      unsavedDrafts.delete(scope)
      draft = undefined
    }
    if (draft) {
      adoptedDraft.current = draft
      if (!draft.conflict && !workspace.dirty && workspace.status === "synced" && workspace.generation === draft.generation && workspace.revision > draft.baseRevision && sameNotebook(stored, draft.data)) {
        unsavedDrafts.delete(scope)
        adoptedDraft.current = null
        setStorageError(false); setSaveError("")
        changed(value => value + 1)
      } else {
        // A restore rotates generation; even an identical server value is not
        // the acknowledgement of this instance's pending write.
        const needsResolution = !workspace.dirty && workspace.status === "synced" && (
          workspace.generation !== draft.generation ||
          (workspace.revision > draft.baseRevision && !sameNotebook(stored, draft.data))
        )
        if (needsResolution && !draft.conflict) {
          draft.conflict = true
          draft.error = "工作区已恢复或 revision 已改变；草稿仍保留，请明确选择保留草稿或采用服务端版本。"
          // The Map and data reference are unchanged, so notify the render
          // explicitly rather than relying on an identical setData value.
          changed(value => value + 1)
        }
        restored = draft.data
        setStorageError(Boolean(draft.error) || workspace.status === "error")
        setSaveError(draft.error)
      }
    } else {
      setStorageError(false); setSaveError("")
      if (!owner && canPersist) {
        try { restored = validLocal(JSON.parse(localStorage.getItem(guestKey) || localStorage.getItem(legacyKey) || "null"), paperId, sourceRevision) ?? restored }
        catch { setStorageError(true); setSaveError("浏览器草稿读取失败。") }
      }
    }
    if (owner) {
      try { setLegacyAvailable(exactSource && Boolean(localStorage.getItem(legacyKey))) }
      catch { setLegacyAvailable(false) }
    }
    current.current = restored
    setData(restored)
    setLoadedScope(scope)
  }, [canPersist, exactSource, guestKey, legacyKey, owner, paperId, scope, sourceRevision, stored, workspace.dirty, workspace.generation, workspace.ready, workspace.revision, workspace.status])

  const persist = useCallback((next: ResearchReaderNotebook): boolean => {
    if (!inScope()) return false
    if (owner) {
      const state = historyState()
      let draft = unsavedDrafts.get(scope)
      if (!draft) {
        draft = { data: next, baseRevision: state.revision, generation: state.generation, baseWorkspace: structuredClone(state.workspace), baseWasClean: !state.dirty, conflict: state.status === "conflict", error: "" }
        unsavedDrafts.set(scope, draft)
      }
      adoptedDraft.current = draft
      draft.data = next
      if (next.note.length > 20000 || !canPersist) {
        draft.error = next.note.length > 20000 ? "笔记超过 20,000 字；草稿已保留，未覆盖服务端内容。" : "来源修订尚未确认；草稿已保留。"
      } else if (state.status === "conflict" || draft.conflict || (!state.dirty && state.status === "synced" && state.generation !== draft.generation)) {
        draft.conflict = true
        draft.error = "工作区存在 revision 冲突；请明确选择保留草稿或采用服务端版本。"
      } else {
        const saved = saveReaderNotebook(next, owner)
        if (saved) draft.generation = historyState().generation
        draft.error = saved ? "" : historyState().message || "笔记未能进入待保存工作区，草稿已保留。"
      }
      setStorageError(Boolean(draft.error)); setSaveError(draft.error); changed(value => value + 1)
      return !draft.error
    }
    try {
      if (!canPersist || next.note.length > 20000) throw new Error("笔记不能保存；当前草稿已保留。")
      localStorage.setItem(guestKey, JSON.stringify(next)); setStorageError(false); setSaveError(""); return true
    } catch (cause) {
      setStorageError(true); setSaveError(cause instanceof Error ? cause.message : "笔记保存失败。"); return false
    }
  }, [canPersist, guestKey, inScope, owner, scope])

  const update = useCallback((patch: NotebookPatch) => {
    if (!ready || !inScope() || resolvingRequest.current) return
    const next = { ...current.current, ...patch, sourceRevision, updatedAt: Math.max(Date.now(), current.current.updatedAt + 1) }
    if (Object.keys(patch).every(field => next[field as keyof ResearchReaderNotebook] === current.current[field as keyof ResearchReaderNotebook])) return
    current.current = next; setData(next); persist(next)
  }, [inScope, persist, ready, sourceRevision])

  const retrySave = useCallback(() => {
    if (!ready || !inScope() || resolvingRequest.current) return false
    const saved = persist(current.current)
    if (saved && owner) window.dispatchEvent(new Event(retryHistoryEvent))
    return saved
  }, [inScope, owner, persist, ready])

  const resolveConflict = useCallback(async (choice: "keep-draft" | "use-server"): Promise<boolean> => {
    const draft = unsavedDrafts.get(scope)
    if (!owner || !draft || !ready || !inScope() || resolvingRequest.current) return false
    const state = historyState()
    // Never discard unrelated workspace edits to resolve one notebook.
    if (!draft.baseWasClean || withoutNotebook(state.workspace, paperId) !== withoutNotebook(draft.baseWorkspace, paperId)) {
      setSaveError("还有其他待保存的工作区改动；不能为解决单篇笔记而丢弃它们。请先处理其他工作区冲突，当前草稿继续保留。")
      return false
    }
    const controller = new AbortController()
    resolvingRequest.current = controller; setResolving(true); setSaveError("")
    const generation = state.generation
    try {
      const response = await fetch("/api/research/history", { credentials: "same-origin", cache: "no-store", signal: controller.signal })
      const payload = await response.json()
      const latest = validateWorkspace(payload.data)
      const revision = payload.data?.revision
      if (!response.ok || !payload.success || !latest || !Number.isSafeInteger(revision) || revision < 0) throw new Error("最新工作区读取失败；草稿仍保留。")
      if (!inScope() || controller.signal.aborted || historyState().generation !== generation) return false
      if (choice === "keep-draft" && (draft.data.note.length > 20000 || !canPersist)) throw new Error("草稿尚不能保存，请先修正超限内容或来源身份。")
      acceptAccountHistory(latest, revision)
      if (choice === "use-server") {
        unsavedDrafts.delete(scope)
        adoptedDraft.current = null
        current.current = restoreNotebook(latest.readerNotebooks?.find(item => item.paperId === paperId), paperId, sourceRevision)
        setData(current.current); setStorageError(false); setSaveError("")
        changed(value => value + 1)
        return true
      }
      draft.baseWorkspace = structuredClone(latest); draft.baseRevision = revision; draft.generation = historyState().generation; draft.baseWasClean = true; draft.conflict = false; draft.error = ""
      const saved = persist(draft.data)
      if (saved) window.dispatchEvent(new Event(retryHistoryEvent))
      return saved
    } catch (cause) {
      if (inScope() && !controller.signal.aborted) setSaveError(cause instanceof Error ? cause.message : "冲突处理失败；草稿仍保留。")
      return false
    } finally {
      if (resolvingRequest.current === controller) { resolvingRequest.current = null; if (inScope()) setResolving(false) }
    }
  }, [canPersist, inScope, owner, paperId, persist, ready, scope, sourceRevision])

  const importLegacy = useCallback(() => {
    if (!owner || !exactSource || !ready || !inScope() || resolvingRequest.current) return false
    try {
      const legacy = validLocal(JSON.parse(localStorage.getItem(legacyKey) || "null"), paperId, sourceRevision)
      if (!legacy) return false
      const merged = { ...legacy, note: current.current.note || legacy.note, question: current.current.question || legacy.question, updatedAt: Date.now() }
      current.current = merged; setData(merged)
      const saved = persist(merged)
      if (saved) setLegacyAvailable(false)
      return saved
    } catch { setStorageError(true); setSaveError("旧草稿读取失败；未覆盖当前笔记。"); return false }
  }, [exactSource, inScope, legacyKey, owner, paperId, persist, ready, sourceRevision])

  const draft = owner ? unsavedDrafts.get(scope) : undefined
  const dirtyDraft = Boolean(draft)
  const visible = loadedScope === scope ? data : emptyNotebook(paperId, sourceRevision)
  useEffect(() => {
    if (!dirtyDraft) return
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = "" }
    window.addEventListener("beforeunload", beforeUnload)
    return () => window.removeEventListener("beforeunload", beforeUnload)
  }, [dirtyDraft])
  const conflict = Boolean(draft?.conflict || (draft && workspace.status === "conflict"))
  const saveStatus = conflict ? "conflict" : storageError || (draft && workspace.status === "error") ? "error" : draft ? "saving" : owner ? workspace.status : "local"
  return { ...visible, update, ready, storageError, saveError, dirty: dirtyDraft, resolving, retrySave, resolveConflict, noteOverLimit: visible.note.length > 20000, resumeSection: visible.sectionId, revisionChanged, legacyAvailable, importLegacy, saveStatus }
}
