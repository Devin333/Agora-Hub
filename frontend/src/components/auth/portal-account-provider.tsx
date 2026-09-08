"use client"

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react"
import { fetchPortalSession, logoutPortal, authErrorMessage } from "@/lib/auth/portal-api"
import type { AuthSession } from "@/lib/papers/types"
import { PortalLoginDialog } from "./portal-login-dialog"

type AccountContext = {
  session: AuthSession | null
  pending: boolean
  error: string | null
  openLogin: (trigger?: HTMLElement) => void
  signOut: () => Promise<void>
}
const Context = createContext<AccountContext | null>(null)

export function PortalAccountProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AuthSession | null>(null)
  const [open, setOpen] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const trigger = useRef<HTMLElement | null>(null)
  const initialSession = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    initialSession.current = controller
    fetchPortalSession(controller.signal).then((result) => { if (!controller.signal.aborted) setSession(result.session) }).catch(() => {})
    return () => controller.abort()
  }, [])
  const openLogin = useCallback((element?: HTMLElement) => {
    trigger.current = element ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null)
    setError(null)
    setOpen(true)
  }, [])
  async function signOut() {
    if (pending) return
    setPending(true)
    initialSession.current?.abort()
    setError(null)
    try { await logoutPortal(); setSession(null) }
    catch (cause) { setError(authErrorMessage(cause)) }
    finally { setPending(false) }
  }
  return <Context.Provider value={{ session, pending, error, openLogin, signOut }}>
    {children}
    <PortalLoginDialog open={open} onOpenChange={setOpen} onAuthenticated={(value) => { initialSession.current?.abort(); setSession(value); setOpen(false) }} onRestoreFocus={() => trigger.current?.focus({ preventScroll: true })} />
  </Context.Provider>
}

export function usePortalAccount() { return useContext(Context) }
