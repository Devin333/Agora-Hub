import path from "node:path"
import { pathToFileURL } from "node:url"

export interface NavigationDecision {
  action: "allow" | "external" | "deny"
  url?: string
}

function parseUrl(raw: string): URL | null {
  try {
    return new URL(raw)
  } catch {
    return null
  }
}

export function isAllowedApplicationUrl(raw: string, canonicalOrigin: string): boolean {
  const url = parseUrl(raw)
  if (!url || url.origin !== canonicalOrigin || url.username || url.password) return false
  return url.pathname === "/login" || url.pathname === "/paper-agent" || url.pathname.startsWith("/paper-agent/")
}

export function isTrustedIpcSource(raw: string, canonicalOrigin: string | null, startupFile: string): boolean {
  const source = parseUrl(raw)
  if (!source || source.username || source.password) return false
  const startupUrl = pathToFileURL(path.resolve(startupFile))
  if (source.protocol === "file:") return source.href === startupUrl.href
  return canonicalOrigin !== null && isAllowedApplicationUrl(source.href, canonicalOrigin)
}

export function decideNavigation(raw: string, canonicalOrigin: string): NavigationDecision {
  if (isAllowedApplicationUrl(raw, canonicalOrigin)) return { action: "allow" }
  const url = parseUrl(raw)
  if (!url || (url.protocol !== "http:" && url.protocol !== "https:")) return { action: "deny" }
  if (url.origin === canonicalOrigin || url.username || url.password) return { action: "deny" }
  return { action: "external", url: url.href }
}

export function paperAgentUrl(canonicalOrigin: string): string {
  return new URL("/paper-agent", canonicalOrigin).href
}
