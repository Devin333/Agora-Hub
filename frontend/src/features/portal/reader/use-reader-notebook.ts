"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { isLocalDesktopProfile } from "@/lib/deployment-profile"
import { historyState, saveReaderNotebook } from "@/lib/research/history"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"
import type { ResearchReaderNotebook } from "@/lib/research/workspace-items"

function emptyNotebook(paperId: string, sourceRevision: string): ResearchReaderNotebook {
  return {
    paperId,
    sourceRevision,
    note: "",
    question: "",
    fontSize: 18,
    sectionId: "",
    pdfPage: 1,
    updatedAt: Date.now(),
  }
}

function validLocal(
  value: unknown,
  paperId: string,
  sourceRevision: string,
): ResearchReaderNotebook | null {
  if (!value || typeof value !== "object") return null
  const item = value as Partial<ResearchReaderNotebook>
  const sameRevision = item.sourceRevision === sourceRevision
  return {
    paperId,
    sourceRevision,
    note: typeof item.note === "string" ? item.note : "",
    question: typeof item.question === "string" ? item.question.slice(0, 2000) : "",
    fontSize: [16, 18, 20, 22].includes(Number(item.fontSize))
      ? item.fontSize as 16 | 18 | 20 | 22
      : 18,
    sectionId: sameRevision && typeof item.sectionId === "string"
      ? item.sectionId.slice(0, 200)
      : "",
    pdfPage: sameRevision && Number.isSafeInteger(item.pdfPage) && Number(item.pdfPage) > 0
      ? Number(item.pdfPage)
      : 1,
    updatedAt: typeof item.updatedAt === "number" ? item.updatedAt : Date.now(),
  }
}

export function useReaderNotebook(paperId: string, sourceRevision = "") {
  const workspace = useOwnedResearchWorkspace()
  const owner = workspace.owner
  const desktopProfile = isLocalDesktopProfile()
  const exactSource = Boolean(sourceRevision)
  const canPersist = !desktopProfile || exactSource
  const guestKey = `agora:reader-notebook:v2:${paperId}:${sourceRevision}`
  const legacyKey = `agora:reader-notebook:v1:${paperId}`
  const current = useRef<ResearchReaderNotebook>(emptyNotebook(paperId, sourceRevision))
  const [data, setData] = useState(current.current)
  const [ready, setReady] = useState(false)
  const [storageError, setStorageError] = useState(false)
  const [legacyAvailable, setLegacyAvailable] = useState(false)
  const stored = useMemo(
    () => workspace.workspace.readerNotebooks?.find(item => item.paperId === paperId),
    [paperId, workspace.workspace.readerNotebooks],
  )
  const revisionChanged = Boolean(exactSource && stored && stored.sourceRevision !== sourceRevision)

  useEffect(() => {
    setReady(false)
    if (!workspace.ready) return
    let restored = emptyNotebook(paperId, sourceRevision)
    if (owner) {
      if (stored) {
        restored = {
          ...stored,
          sourceRevision,
          sectionId: exactSource && stored.sourceRevision === sourceRevision ? stored.sectionId : "",
          pdfPage: exactSource && stored.sourceRevision === sourceRevision ? stored.pdfPage : 1,
        }
      }
      try {
        setLegacyAvailable(exactSource && Boolean(localStorage.getItem(legacyKey)))
      } catch {
        setLegacyAvailable(false)
        setStorageError(true)
      }
    } else if (canPersist) {
      try {
        const local = JSON.parse(
          localStorage.getItem(guestKey) || localStorage.getItem(legacyKey) || "null",
        )
        restored = validLocal(local, paperId, sourceRevision) ?? restored
      } catch {
        setStorageError(true)
      }
    }
    current.current = restored
    setData(restored)
    setReady(true)
  }, [canPersist, exactSource, guestKey, legacyKey, owner, paperId, sourceRevision, stored, workspace.ready])

  const persist = useCallback((next: ResearchReaderNotebook) => {
    if (next.note.length > 20000) {
      setStorageError(true)
      return false
    }
    if (!canPersist) {
      setStorageError(true)
      return false
    }
    if (owner) {
      const saved = saveReaderNotebook(next, owner)
      setStorageError(!saved)
      return saved
    }
    try {
      localStorage.setItem(guestKey, JSON.stringify(next))
      setStorageError(false)
      return true
    } catch {
      setStorageError(true)
      return false
    }
  }, [canPersist, guestKey, owner])

  const update = useCallback((
    patch: Partial<Pick<ResearchReaderNotebook, "note" | "question" | "fontSize" | "sectionId" | "pdfPage">>,
  ) => {
    if (!ready || historyState().owner !== owner) return
    const next = { ...current.current, ...patch, sourceRevision, updatedAt: Date.now() }
    if (Object.keys(patch).every(
      field => next[field as keyof ResearchReaderNotebook] === current.current[field as keyof ResearchReaderNotebook],
    )) return
    current.current = next
    setData(next)
    persist(next)
  }, [owner, persist, ready, sourceRevision])

  const importLegacy = useCallback(() => {
    if (!owner || !exactSource || historyState().owner !== owner) return false
    try {
      const legacy = validLocal(
        JSON.parse(localStorage.getItem(legacyKey) || "null"),
        paperId,
        sourceRevision,
      )
      if (!legacy) return false
      const merged = {
        ...legacy,
        note: data.note || legacy.note,
        question: data.question || legacy.question,
        updatedAt: Date.now(),
      }
      if (!saveReaderNotebook(merged, owner)) {
        current.current = merged
        setData(merged)
        setStorageError(true)
        return false
      }
      current.current = merged
      setData(merged)
      setLegacyAvailable(false)
      return true
    } catch {
      setStorageError(true)
      return false
    }
  }, [data.note, data.question, exactSource, legacyKey, owner, paperId, sourceRevision])

  return {
    ...data,
    update,
    ready,
    storageError,
    noteOverLimit: data.note.length > 20000,
    resumeSection: data.sectionId,
    revisionChanged,
    legacyAvailable,
    importLegacy,
    saveStatus: owner ? workspace.status : storageError ? "error" : "local",
  }
}
