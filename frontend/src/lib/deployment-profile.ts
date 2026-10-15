export const DEPLOYMENT_PROFILES = ["default", "local-desktop"] as const
export type DeploymentProfile = (typeof DEPLOYMENT_PROFILES)[number]
const DEFAULT_DESKTOP_ORIGIN = "http://127.0.0.1:3000"
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"])

export function resolveDeploymentProfile(value = process.env.NEWSROOM_DEPLOYMENT_PROFILE ?? process.env.NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE): DeploymentProfile {
  const normalized = (value ?? "default").trim()
  if (normalized === "default" || normalized === "local-desktop") return normalized
  throw new Error(`Unsupported NEWSROOM_DEPLOYMENT_PROFILE: ${normalized}`)
}
export function isLocalDesktopProfile(value = process.env.NEWSROOM_DEPLOYMENT_PROFILE ?? process.env.NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE) { return resolveDeploymentProfile(value) === "local-desktop" }
export function serverOwnedMutationOriginHeaders(): Record<string, string> {
  return isLocalDesktopProfile() ? { Origin: desktopCanonicalOrigin() } : {}
}
export function desktopCanonicalOrigin(value?: string): string {
  const canonical = process.env.NEWSROOM_CANONICAL_ORIGIN
  const legacyDesktop = process.env.NEWSROOM_DESKTOP_CANONICAL_ORIGIN
  if (value === undefined && canonical && legacyDesktop && canonical.replace(/\/$/, "") !== legacyDesktop.replace(/\/$/, "")) {
    throw new Error("NEWSROOM_CANONICAL_ORIGIN and NEWSROOM_DESKTOP_CANONICAL_ORIGIN must match")
  }
  const origin = (value || canonical || legacyDesktop || DEFAULT_DESKTOP_ORIGIN).trim()
  let parsed: URL
  try { parsed = new URL(origin) } catch { throw new Error("NEWSROOM_DESKTOP_CANONICAL_ORIGIN must be an absolute URL") }
  if (parsed.origin !== origin.replace(/\/$/, "") || parsed.username || parsed.password || parsed.pathname !== "/" || parsed.search || parsed.hash) throw new Error("NEWSROOM_DESKTOP_CANONICAL_ORIGIN must contain only scheme, loopback host, and port")
  if (!LOOPBACK_HOSTS.has(parsed.hostname)) throw new Error("local-desktop canonical origin must use a loopback host")
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("local-desktop canonical origin must use HTTP or HTTPS")
  return parsed.origin
}
export function localDesktopAllowsInsecureCookie() { return isLocalDesktopProfile() && new URL(desktopCanonicalOrigin()).protocol === "http:" }
export function isConfiguredDesktopOrigin(origin: string | null) { if (!origin || !isLocalDesktopProfile()) return false; try { return new URL(origin).origin === desktopCanonicalOrigin() } catch { return false } }

export function validateLocalDesktopServiceOrigin(value: string): string {
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    throw new Error("local-desktop service origin must be an absolute URL")
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    !LOOPBACK_HOSTS.has(parsed.hostname) ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash ||
    parsed.origin !== value.replace(/\/$/, "")
  ) {
    throw new Error("local-desktop service origin must contain only HTTP(S), loopback host, and port")
  }
  return parsed.origin
}
