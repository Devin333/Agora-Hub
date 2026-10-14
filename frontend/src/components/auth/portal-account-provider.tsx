"use client"

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react"
import { fetchPortalSession, logoutPortal, authErrorMessage } from "@/lib/auth/portal-api"
import { portalSessionChangedEvent } from "@/lib/auth/session-events"
import type { AuthSession } from "@/lib/papers/types"
import { PortalLoginDialog } from "./portal-login-dialog"

export { portalSessionChangedEvent } from "@/lib/auth/session-events"

type AccountContext = {
  session: AuthSession | null
  resolved: boolean
  pending: boolean
  error: string | null
  openLogin: (trigger?: HTMLElement) => void
  signOut: () => Promise<void>
}

const Context = createContext<AccountContext | null>(null)

export function PortalAccountProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AuthSession | null>(null)
  const [resolved, setResolved] = useState(false)
  const [open, setOpen] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const trigger = useRef<HTMLElement | null>(null)
  const sessionRequest = useRef<AbortController | null>(null)

  useEffect(() => {
    let mounted = true
    const refreshSession = () => {
      sessionRequest.current?.abort()
      const controller = new AbortController()
      sessionRequest.current = controller
      setResolved(false)
      setError(null)
      fetchPortalSession(controller.signal)
        .then(result => {
          if (mounted && !controller.signal.aborted) {
            setSession(result.session)
            setResolved(true)
          }
        })
        .catch(() => {
          if (mounted && !controller.signal.aborted) {
            setSession(null)
            setResolved(true)
            setError("账号状态暂时无法读取，请重新登录。")
          }
        })
    }

    window.addEventListener(portalSessionChangedEvent, refreshSession)
    refreshSession()
    return () => {
      mounted = false
      window.removeEventListener(portalSessionChangedEvent, refreshSession)
      sessionRequest.current?.abort()
      sessionRequest.current = null
    }
  }, [])

  const openLogin = useCallback((element?: HTMLElement) => {
    trigger.current = element ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null)
    setError(null)
    setOpen(true)
  }, [])

  async function signOut() {
    if (pending) return
    setPending(true)
    sessionRequest.current?.abort()
    setError(null)
    try { await logoutPortal(); setSession(null); setResolved(true) }
    catch (cause) { setError(authErrorMessage(cause)) }
    finally { setPending(false) }
  }

  return <Context.Provider value={{ session, resolved, pending, error, openLogin, signOut }}>
    {children}
    <PortalLoginDialog open={open} onOpenChange={setOpen} onAuthenticated={(value) => { sessionRequest.current?.abort(); setSession(value); setResolved(true); setError(null); setOpen(false) }} onRestoreFocus={() => trigger.current?.focus({ preventScroll: true })} />
  </Context.Provider>
}

export function usePortalAccount() { return useContext(Context) }
