import { NextRequest, NextResponse } from "next/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { getFrontendSurface, type FrontendSurface } from "@/lib/frontend-surface"
import { isLocalDesktopProfile } from "@/lib/deployment-profile"
import { migrateLegacyResearchPath, safeLocalNavigation } from "@/lib/research/routes"

const PORTAL_PREFIXES = ["/news", "/topics", "/tech", "/reports", "/search", "/papers", "/community", "/research"]
const ADMIN_PREFIXES = ["/studio", "/admin"]

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl
  const surface = getFrontendSurface()
  const desktop = isLocalDesktopProfile()

  if (desktop && pathname.startsWith("/design-demo")) {
    const migrated = migrateLegacyResearchPath(`${pathname}${search}`)
    if (migrated) return NextResponse.redirect(new URL(migrated, request.nextUrl.origin), 308)
  }

  if (isTemporaryFrontendAuthDisabled() && isLoginPath(pathname)) return redirectToLoginBypassTarget(request, surface)
  if (isPublicPath(pathname, surface, desktop)) return NextResponse.next()

  const surfaceRedirect = getCrossSurfaceRedirect(request, surface)
  if (surfaceRedirect) return surfaceRedirect
  if (!isProtectedPath(pathname)) return NextResponse.next()

  if (isTemporaryFrontendAuthDisabled()) {
    if (surface === "admin" && pathname === "/") return redirectPath(request, "/studio")
    return NextResponse.next()
  }

  if (request.cookies.get(NEWSROOM_SESSION_COOKIE)?.value) {
    if (surface === "admin" && pathname === "/") return redirectPath(request, "/studio")
    return NextResponse.next()
  }

  const loginUrl = request.nextUrl.clone()
  loginUrl.pathname = "/login"
  loginUrl.search = ""
  loginUrl.searchParams.set("next", `${pathname}${search}`)
  return NextResponse.redirect(loginUrl)
}

function isTemporaryFrontendAuthDisabled() {
  return !isLocalDesktopProfile() && process.env.NEWSROOM_ENABLE_FRONTEND_AUTH !== "true"
}
function isLoginPath(pathname: string) { return pathname === "/login" || pathname.startsWith("/login/") }
function isPublicPath(pathname: string, surface: FrontendSurface, desktop: boolean) {
  return (!desktop && surface === "portal" && (pathname === "/" || isResearchReadPath(pathname))) || isLoginPath(pathname) || pathname.startsWith("/api/") || pathname.startsWith("/_next/") || pathname === "/favicon.ico"
}
function isResearchReadPath(pathname: string) { return pathname === "/papers" || pathname.startsWith("/papers/") }
function isProtectedPath(pathname: string) { return pathname === "/" || isAdminPath(pathname) || isPortalPath(pathname) }
function isPortalPath(pathname: string) { return isPaperAgentPath(pathname) || PORTAL_PREFIXES.some(prefix => pathname === prefix || pathname.startsWith(`${prefix}/`)) }
function isPaperAgentPath(pathname: string) { return pathname === "/paper-agent" || pathname === "/paper-agent/" }
function isAdminPath(pathname: string) { return ADMIN_PREFIXES.some(prefix => pathname === prefix || pathname.startsWith(`${prefix}/`)) }
function getCrossSurfaceRedirect(request: NextRequest, surface: FrontendSurface) {
  const { pathname, search } = request.nextUrl
  if (surface === "portal" && isAdminPath(pathname)) return redirectToSurface(request, process.env.NEWSROOM_ADMIN_ORIGIN, `${pathname}${search}`, "/")
  if (surface === "admin" && isPortalPath(pathname)) return redirectToSurface(request, process.env.NEWSROOM_PORTAL_ORIGIN, `${pathname}${search}`, "/")
  return null
}
function redirectToSurface(request: NextRequest, origin: string | undefined, targetPath: string, fallbackPath: string) {
  if (origin) { try { return NextResponse.redirect(new URL(targetPath, origin)) } catch { /* use local fallback */ } }
  return redirectPath(request, fallbackPath)
}
function redirectPath(request: NextRequest, pathname: string) { const url = request.nextUrl.clone(); url.pathname = pathname; url.search = ""; return NextResponse.redirect(url) }
function redirectToLoginBypassTarget(request: NextRequest, surface: FrontendSurface) {
  const targetPath = safeNextPath(request.nextUrl.searchParams.get("next"), surface === "admin" ? "/studio" : "/")
  return NextResponse.redirect(new URL(targetPath, request.nextUrl.origin))
}
function safeNextPath(value: string | null, fallback: string) { return value && safeLocalNavigation(value) && !isLoginPath(value) ? value : fallback }

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] }
