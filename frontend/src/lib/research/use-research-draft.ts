"use client"

import { useEffect, useRef, useState } from "react"
import { isResearchMode, type ResearchMode } from "./entry"

const key = "agora-home-draft:v1"
export function useResearchDraft() {
  const [query, setQuery] = useState("")
  const [mode, setMode] = useState<ResearchMode>("auto")
  const [ready, setReady] = useState(false)
  const current = useRef({ query, mode })
  current.current = { query, mode }
  useEffect(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(key) ?? "null")
      if (typeof saved?.query === "string" && saved.query.length <= 2000 && isResearchMode(saved.mode)) {
        setQuery(saved.query)
        setMode(saved.mode)
      }
    } catch { /* Optional local draft. */ }
    setReady(true)
    const save = () => { try { sessionStorage.setItem(key, JSON.stringify(current.current)) } catch { /* Navigation stays available. */ } }
    window.addEventListener("pagehide", save)
    window.addEventListener("agora-auth-save-context", save)
    return () => {
      window.removeEventListener("pagehide", save)
      window.removeEventListener("agora-auth-save-context", save)
    }
  }, [])
  useEffect(() => {
    if (ready) try { sessionStorage.setItem(key, JSON.stringify({ query, mode })) } catch { /* Optional local draft. */ }
  }, [ready, query, mode])
  return { query, setQuery, mode, setMode, ready }
}
