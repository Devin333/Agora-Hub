import { apiGet, apiPost } from "@/lib/api/client"
import { notifyPortalSessionChanged } from "@/lib/auth/session-events"
import type { AuthSession } from "@/lib/papers/types"

type ApiEnvelope<T> = {
  success: boolean
  data?: T | null
  error?: {
    code: string
    message: string
    requestId?: string
    request_id?: string | null
  } | null
}

export type AuthSessionStatus = {
  initialized: boolean
  // Only a fresh default-profile install offers browser bootstrap; a hosted
  // service creates its first administrator from the server CLI.
  bootstrapAvailable: boolean
  session: AuthSession | null
}

export async function fetchAuthSession(init?: RequestInit): Promise<AuthSessionStatus> {
  const envelope = await apiGet<ApiEnvelope<AuthSessionStatus>>("/api/auth/session", init)
  return unwrapEnvelope(envelope)
}

export async function bootstrapAccount(username: string, password: string, init?: RequestInit): Promise<AuthSession> {
  const envelope = await apiPost<ApiEnvelope<{ session: AuthSession }>>(
    "/api/auth/bootstrap",
    { username, password },
    init
  )
  const session = unwrapEnvelope(envelope).session
  notifyPortalSessionChanged()
  return session
}

export async function login(username: string, password: string, init?: RequestInit): Promise<AuthSession> {
  const envelope = await apiPost<ApiEnvelope<{ session: AuthSession }>>(
    "/api/auth/login",
    { username, password },
    init
  )
  const session = unwrapEnvelope(envelope).session
  notifyPortalSessionChanged()
  return session
}

export async function logout(init?: RequestInit): Promise<void> {
  const envelope = await apiPost<ApiEnvelope<{ revoked: boolean }>>("/api/auth/logout", undefined, init)
  unwrapEnvelope(envelope)
  notifyPortalSessionChanged()
}

function unwrapEnvelope<T>(envelope: ApiEnvelope<T>): T {
  if (envelope.success && envelope.data) {
    return envelope.data
  }
  throw new Error(envelope.error?.message ?? "Auth request failed")
}
