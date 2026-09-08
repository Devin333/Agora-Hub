"use client"

import { useEffect, useState } from "react"
import { historyState, researchHistoryEvent } from "./history"
import { usePortalAccount } from "@/components/auth/portal-account-provider"

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

/** Hide the previous owner during the render before the sync effect switches stores. */
export function useOwnedResearchWorkspace(): ReturnType<typeof historyState> {
  const state = useResearchHistory()
  const account = usePortalAccount()
  const expectedOwner = account ? account.resolved ? account.session?.user.userId ?? null : undefined : state.owner
  if (expectedOwner === undefined || expectedOwner !== state.owner) return {
    ...state, owner: expectedOwner, ready: false, workspace: { visits: [], groups: [] },
    status: account?.error ? "error" as const : "loading" as const, message: account?.error ?? "",
  }
  return state
}
