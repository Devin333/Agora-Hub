"use client"

import { useEffect, useRef } from "react"

const eventName = "agora-auth-save-context"
const storagePrefix = "agora-auth-return:"

export function preserveResearchForRedirect() {
  window.dispatchEvent(new Event(eventName))
}

// Only research drafts are saved, never login identifiers, verification codes, or tokens.
export function useResearchReturnContext<T>(key: string, value: T, restore: (value: T) => void) {
  const current = useRef(value)
  const restoreRef = useRef(restore)
  current.current = value
  restoreRef.current = restore
  useEffect(() => {
    const storageKey = storagePrefix + key
    const path = () => window.location.pathname + window.location.search
    let restoreFrame: number | undefined
    try {
      const raw = sessionStorage.getItem(storageKey)
      if (raw) {
        const saved = JSON.parse(raw)
        const age = Date.now() - saved.createdAt
        if (saved.path === path() && Number.isFinite(age) && age >= 0 && age < 900000) {
          // Restore after page initialization effects, including React StrictMode's replay.
          restoreFrame = requestAnimationFrame(() => {
            restoreRef.current(saved.value)
            window.scrollTo({ top: Number(saved.scrollY) || 0, behavior: "instant" })
            try { sessionStorage.removeItem(storageKey) } catch { /* Storage may become unavailable. */ }
          })
        } else sessionStorage.removeItem(storageKey)
      }
    } catch { /* Storage can be unavailable in privacy-restricted browsers. */ }
    const save = () => {
      try { sessionStorage.setItem(storageKey, JSON.stringify({ path: path(), value: current.current, scrollY: window.scrollY, createdAt: Date.now() })) } catch { /* The URL still preserves applied research filters. */ }
    }
    window.addEventListener(eventName, save)
    return () => {
      window.removeEventListener(eventName, save)
      if (restoreFrame !== undefined) cancelAnimationFrame(restoreFrame)
    }
  }, [key])
}
