"use client"

import type { ReactNode } from "react"
import { usePathname } from "next/navigation"
import { ResearchHeader } from "@/components/layout/research-header"
import type { FrontendSurface } from "@/lib/frontend-surface"
import { useUiStore } from "@/stores/ui-store"
import { cn } from "@/lib/utils"
import { isLocalDesktopProfile } from "@/lib/deployment-profile"

export function AppShell({ children, surface = "portal" }: { children: ReactNode; surface?: FrontendSurface }) {
  const pathname = usePathname()
  const theme = useUiStore((state) => state.theme)
  const locale = useUiStore((state) => state.locale)
  const setTheme = useUiStore((state) => state.setTheme)
  const setLocale = useUiStore((state) => state.setLocale)

  if (surface === "admin") {
    return <div className="min-h-screen bg-background text-foreground">{children}</div>
  }

  if (pathname.startsWith("/admin") || pathname.startsWith("/studio")) {
    return <div className="min-h-screen bg-background text-foreground">{children}</div>
  }

  const isPaperAgentLabRoute = pathname === "/paper-agent-lab" || pathname === "/paper-agent-lab/"
  const isPaperAgentRoute = pathname === "/paper-agent" || pathname === "/paper-agent/"
  const isLocalDesktopLoginRoute = isLocalDesktopProfile() && (pathname === "/login" || pathname === "/login/")
  const isPortalHomeRoute = pathname === "/"
  const isDesktopResearchRoute = isLocalDesktopProfile() && (pathname === "/" || pathname === "/papers" || pathname === "/reports" || pathname.startsWith("/reports/") || pathname.startsWith("/research/groups/"))
  const isReaderRoute = isPaperReaderRoute(pathname)
  const isDesignDemoRoute = pathname.startsWith("/design-demo")
  const isTaxonomyDirectory = pathname === "/papers/methods" || pathname === "/papers/tasks"
  const usesResearchFrame = pathname.startsWith("/papers") || pathname.startsWith("/projects") || isPortalHomeRoute

  if (isPaperAgentLabRoute || isPaperAgentRoute || isLocalDesktopLoginRoute) {
    return <div className="min-h-screen">{children}</div>
  }

  if (isReaderRoute && !isTaxonomyDirectory) {
    return <div className="min-h-screen bg-background text-foreground">{children}</div>
  }

  if (isDesignDemoRoute || isTaxonomyDirectory || isDesktopResearchRoute) {
    return <div className="min-h-screen bg-[#f5f6f3] text-[#18231f]">{children}</div>
  }

  return (
    <div className={cn("min-h-screen text-foreground", usesResearchFrame ? "font-papers-research bg-[#f6f7f4] dark:bg-background" : "bg-background")}>
      <ResearchHeader locale={locale} pathname={pathname} theme={theme} onLocaleChange={setLocale} onThemeChange={setTheme} />
      <main className={cn("min-w-0", usesResearchFrame ? "px-4 pb-16 pt-0 sm:px-6 lg:px-8" : "px-4 py-6 sm:px-6")}>
        <div className={cn("mx-auto w-full", usesResearchFrame ? "max-w-[1440px]" : "max-w-[1480px]")}>{children}</div>
      </main>
    </div>
  )
}

function isPaperReaderRoute(pathname: string) {
  const parts = pathname.split("/").filter(Boolean)
  return parts[0] === "papers" && (
    parts.length === 2 ||
    (parts.length === 3 && parts[2] === "read")
  )
}
