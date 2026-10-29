import { connectionFailure, describeHttpStatus, describeNetworkError, type ConnectionFailure } from "./load-failure"

/**
 * Checks that an origin serves the hosted Agora web client before the window
 * is pointed at it, so a typo, a stopped service or a foreign site is reported
 * on the connection page instead of being rendered as someone else's page.
 */
export const HEALTH_PATH = "/api/health"
export const EXPECTED_SERVICE = "agora-hub-frontend"
export const EXPECTED_PROFILE = "hosted"
export const PROBE_TIMEOUT_MS = 10_000

export type ProbeFetch = (url: string, init: { signal: AbortSignal; redirect: "manual"; cache: "no-store"; headers: Record<string, string> }) => Promise<{ status: number; text(): Promise<string> }>

/** Classifies a health answer; null means the origin is a hosted Agora service. */
export function classifyHealthResponse(status: number, body: string): ConnectionFailure | null {
  const statusFailure = describeHttpStatus(status)
  if (statusFailure) return statusFailure
  if (status < 200 || status >= 300) return connectionFailure("not_agora")
  let document: unknown
  try {
    document = JSON.parse(body)
  } catch {
    return connectionFailure("not_agora")
  }
  const data = document && typeof document === "object" ? (document as { data?: unknown }).data : null
  if (!data || typeof data !== "object") return connectionFailure("not_agora")
  const health = data as Record<string, unknown>
  if (health.service !== EXPECTED_SERVICE || health.deploymentProfile !== EXPECTED_PROFILE) return connectionFailure("not_agora")
  return health.ready === true ? null : connectionFailure("server_unavailable")
}

export async function probeServer(fetch: ProbeFetch, origin: string, timeoutMs = PROBE_TIMEOUT_MS): Promise<ConnectionFailure | null> {
  const signal = AbortSignal.timeout(timeoutMs)
  try {
    const response = await fetch(new URL(HEALTH_PATH, origin).href, { signal, redirect: "manual", cache: "no-store", headers: { Accept: "application/json" } })
    return classifyHealthResponse(response.status, await response.text())
  } catch (error) {
    if (signal.aborted) return connectionFailure("timeout")
    return describeNetworkError(error instanceof Error ? error.message : String(error))
  }
}
