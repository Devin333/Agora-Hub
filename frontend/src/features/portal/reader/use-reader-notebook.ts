"use client"

import { useCallback, useEffect, useRef, useState } from "react"

type Notebook = { note: string; question: string; fontSize: number; sectionId: string; pdfPage: number }
const emptyNotebook: Notebook = { note: "", question: "", fontSize: 18, sectionId: "", pdfPage: 1 }

export function useReaderNotebook(paperId: string) {
  const key = `agora:reader-notebook:v1:${paperId}`
  const current = useRef<Notebook>(emptyNotebook)
  const [data, setData] = useState<Notebook>(emptyNotebook)
  const [ready, setReady] = useState(false)
  const [storageError, setStorageError] = useState(false)
  const [resumeSection, setResumeSection] = useState("")

  useEffect(() => {
    let restored = emptyNotebook
    try {
      const saved = JSON.parse(localStorage.getItem(key) || "null")
      if (saved && typeof saved === "object") {
        restored = {
          note: typeof saved.note === "string" ? saved.note.slice(0, 20000) : "",
          question: typeof saved.question === "string" ? saved.question.slice(0, 2000) : "",
          fontSize: [16, 18, 20, 22].includes(saved.fontSize) ? saved.fontSize : 18,
          sectionId: typeof saved.sectionId === "string" ? saved.sectionId : "",
          pdfPage: Number.isSafeInteger(saved.pdfPage) && saved.pdfPage > 0 ? saved.pdfPage : 1,
        }
      }
    } catch { setStorageError(true) }
    current.current = restored
    setData(restored)
    setResumeSection(restored.sectionId)
    setReady(true)
  }, [key])

  const update = useCallback((patch: Partial<Notebook>) => {
    if (!ready) return
    const next = { ...current.current, ...patch }
    if (Object.keys(patch).every((field) => next[field as keyof Notebook] === current.current[field as keyof Notebook])) return
    current.current = next
    setData(next)
    try {
      localStorage.setItem(key, JSON.stringify(next))
      setStorageError(false)
    } catch { setStorageError(true) }
  }, [key, ready])

  return { ...data, update, ready, storageError, resumeSection }
}
