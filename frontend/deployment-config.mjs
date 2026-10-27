// Deployment settings shared by next.config.mjs (build and server start) and
// the server runtime, so both refuse the same unsafe configuration.

export const DEPLOYMENT_PROFILES = ["default", "hosted"]
const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
const TRUE_FLAGS = new Set(["1", "true", "yes"])
const FALSE_FLAGS = new Set(["", "0", "false", "no"])

export function resolveDeploymentProfile(value) {
  const normalized = String(value ?? "default").trim() || "default"
  if (DEPLOYMENT_PROFILES.includes(normalized)) return normalized
  throw new Error(`Unsupported NEWSROOM_DEPLOYMENT_PROFILE: ${normalized}`)
}

export function insecureOriginAllowed(env = process.env) {
  const raw = String(env.NEWSROOM_ALLOW_INSECURE_ORIGIN ?? "").trim().toLowerCase()
  if (TRUE_FLAGS.has(raw)) return true
  if (FALSE_FLAGS.has(raw)) return false
  throw new Error("NEWSROOM_ALLOW_INSECURE_ORIGIN must be 1 or 0")
}

// The browser-facing origin of the hosted service. It must be https unless
// local integration explicitly opts into plain http.
export function resolvePublicOrigin(env = process.env) {
  const value = String(env.NEWSROOM_PUBLIC_ORIGIN ?? "").trim()
  if (!value) throw new Error("NEWSROOM_PUBLIC_ORIGIN is required in the hosted profile")
  const parsed = pureOrigin(value, "NEWSROOM_PUBLIC_ORIGIN")
  if (parsed.protocol === "https:") return parsed.origin
  if (parsed.protocol === "http:" && insecureOriginAllowed(env)) return parsed.origin
  throw new Error("NEWSROOM_PUBLIC_ORIGIN must use https (set NEWSROOM_ALLOW_INSECURE_ORIGIN=1 only for local integration)")
}

// The API is reached only from the server side, usually over a private network.
export function resolveApiBaseUrl(env = process.env) {
  const value = String(env.NEWSROOM_API_BASE_URL ?? DEFAULT_API_BASE_URL).trim()
  const parsed = pureOrigin(value, "NEWSROOM_API_BASE_URL")
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("NEWSROOM_API_BASE_URL must use http or https")
  }
  return parsed.origin
}

function pureOrigin(value, name) {
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    throw new Error(`${name} must be an absolute URL`)
  }
  if (
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash ||
    parsed.origin !== value.replace(/\/$/, "").toLowerCase()
  ) {
    throw new Error(`${name} must contain only scheme, host and port`)
  }
  return parsed
}
