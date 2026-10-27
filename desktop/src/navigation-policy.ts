import path from "node:path"
import { pathToFileURL } from "node:url"

/**
 * What the one window may show and which documents may use which bridge.
 *
 * The window shows either the packaged connection page or the workspace pages
 * (`/login`, `/paper-agent…`) of the configured server origin. Any other web
 * link opens in the user's browser; every other scheme is refused.
 */
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

export function isWorkspaceUrl(raw: string, origin: string): boolean {
  const url = parseUrl(raw)
  if (!url || url.origin !== origin || url.username || url.password) return false
  return url.pathname === "/login" || url.pathname === "/paper-agent" || url.pathname.startsWith("/paper-agent/")
}

export function connectionPageUrl(connectionFile: string): string {
  return pathToFileURL(path.resolve(connectionFile)).href
}

/** Exactly the packaged connection page, with or without a fragment. */
export function isConnectionPage(raw: string, connectionFile: string): boolean {
  const url = parseUrl(raw)
  if (!url || url.protocol !== "file:") return false
  url.hash = ""
  return url.href === connectionPageUrl(connectionFile)
}

/**
 * `origin` is the server the window is connected or connecting to, or null
 * while the connection page is showing: then nothing but the page itself loads.
 */
export function decideNavigation(raw: string, origin: string | null, connectionFile: string): NavigationDecision {
  if (isConnectionPage(raw, connectionFile)) return { action: "allow" }
  if (origin && isWorkspaceUrl(raw, origin)) return { action: "allow" }
  const url = parseUrl(raw)
  if (!url || (url.protocol !== "http:" && url.protocol !== "https:")) return { action: "deny" }
  // The client shows the workspace only; other pages of the service are neither shown nor handed off.
  if (url.origin === origin || url.username || url.password) return { action: "deny" }
  return { action: "external", url: url.href }
}
