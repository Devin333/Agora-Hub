import { isHostedProfile } from "@/lib/deployment-profile"

const LEGACY_PREFIX = "/design-demo"
const SAFE_PATHS = ["/", "/papers", "/projects", "/community", "/reports", "/research/groups"]
const SAFE_RETURN_PREFIXES = ["/", "/papers", "/reports", "/research/groups"]

export function safeLocalNavigation(value: string): boolean {
  if (!value.startsWith("/") || value.startsWith("//") || /[\\\r\n\x00-\x1f]/.test(value) || value.length > 10000) return false
  try {
    const url = new URL(value, "https://agora.invalid")
    return url.origin === "https://agora.invalid" && !url.username && !url.password
  } catch { return false }
}

export function safeResearchReturnTo(value: string | null | undefined, fallback = "/papers") {
  if (!value || !safeLocalNavigation(value)) return fallback
  const url = new URL(value, "https://agora.invalid")
  return SAFE_RETURN_PREFIXES.some(path => url.pathname === path || (path !== "/" && url.pathname.startsWith(`${path}/`))) ? `${url.pathname}${url.search}${url.hash}` : fallback
}

export function migrateLegacyResearchPath(value: string): string | null {
  if (!safeLocalNavigation(value)) return null
  const url = new URL(value, "https://agora.invalid")
  if (url.pathname === LEGACY_PREFIX) url.pathname = "/"
  else if (url.pathname === `${LEGACY_PREFIX}/papers`) url.pathname = "/papers"
  else if (/^\/design-demo\/papers\/[^/]+\/read$/.test(url.pathname)) url.pathname = url.pathname.replace(LEGACY_PREFIX, "")
  else if (/^\/design-demo\/groups\/[^/]+$/.test(url.pathname)) url.pathname = url.pathname.replace(`${LEGACY_PREFIX}/groups`, "/research/groups")
  else return null
  const returnTo = url.searchParams.get("returnTo")
  if (returnTo) url.searchParams.set("returnTo", normalizeResearchHref(returnTo) ?? safeResearchReturnTo(returnTo))
  return `${url.pathname}${url.search}${url.hash}`
}

export function normalizeResearchHref(value: string): string | null {
  const migrated = migrateLegacyResearchPath(value)
  if (migrated) return migrated
  if (!safeLocalNavigation(value)) return null
  const url = new URL(value, "https://agora.invalid")
  const allowed = SAFE_PATHS.some(path => url.pathname === path || (path !== "/" && url.pathname.startsWith(`${path}/`)))
  if (!allowed) return null
  const returnTo = url.searchParams.get("returnTo")
  if (returnTo) url.searchParams.set("returnTo", safeResearchReturnTo(returnTo))
  return `${url.pathname}${url.search}${url.hash}`
}

export function researchHomeHref() { return isHostedProfile() ? "/" : "/design-demo" }
export function researchPapersHref() { return isHostedProfile() ? "/papers" : "/design-demo/papers" }
export function researchReaderHref(slug: string) { return `${researchPapersHref()}/${encodeURIComponent(slug)}/read` }
export function researchGroupHref(groupId: string | null) { return `${isHostedProfile() ? "/research/groups" : "/design-demo/groups"}/${encodeURIComponent(groupId ?? "ungrouped")}` }
