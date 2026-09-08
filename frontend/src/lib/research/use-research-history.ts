"use client"

import { useEffect, useState } from "react"
import { historyState, researchHistoryEvent } from "./history"

export function useResearchHistory() {
  const [state, setState] = useState(historyState)
  useEffect(() => {
    const update = () => setState(historyState())
    update()
    window.addEventListener(researchHistoryEvent, update); window.addEventListener("storage", update)
    return () => { window.removeEventListener(researchHistoryEvent, update); window.removeEventListener("storage", update) }
  }, [])
  return state
}
