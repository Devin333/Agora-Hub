"use client"

import * as React from "react"
import { usePathname } from "next/navigation"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { TooltipProvider } from "@/components/ui/tooltip"
import { useUiStore } from "@/stores/ui-store"
import { applyResolvedTheme, resolveTheme, systemPrefersLight, watchSystemAppearance } from "@/lib/theme/appearance"
import { ResearchHistoryTracker } from "@/components/research/research-history-tracker"
import { PortalAccountProvider, usePortalAccount } from "@/components/auth/portal-account-provider"
import { startHistorySync } from "@/lib/research/history-sync"
import { setHistoryStatus } from "@/lib/research/history"
import { ResearchCommandPalette } from "@/features/portal/components/research-command-palette"
import { researchHomeHref } from "@/lib/research/routes"
import { isLocalDesktopProfile } from "@/lib/deployment-profile"

function ResearchHistoryRuntime() {
  const account = usePortalAccount()
  const owner = account?.resolved ? account.session?.user.userId ?? null : undefined
  const sessionId = account?.session?.sessionId
  React.useEffect(() => startHistorySync(owner), [owner, sessionId])
  React.useEffect(() => { if (owner === undefined && account?.error) setHistoryStatus("error", account.error) }, [owner, account?.error])
  return <React.Suspense fallback={null}><ResearchHistoryTracker enabled={owner !== undefined} expectedOwner={owner} /></React.Suspense>
}

function ThemeBridge() {
  const appearance = useUiStore((state) => state.appearance)
  const setResolvedTheme = useUiStore((state) => state.setResolvedTheme)
  const locale = useUiStore((state) => state.locale)

  React.useEffect(() => {
    const apply = (prefersLight: boolean) => {
      const theme = resolveTheme(appearance, prefersLight)
      applyResolvedTheme(theme)
      setResolvedTheme(theme)
    }
    apply(systemPrefersLight())
    return appearance === "system" ? watchSystemAppearance(apply) : undefined
  }, [appearance, setResolvedTheme])

  React.useEffect(() => {
    document.documentElement.lang = locale === "zh" ? "zh-CN" : "en"
  }, [locale])

  return null
}

function GlobalResearchCommands() {
  const pathname = usePathname()
  const isPaperReader = /^\/papers\/[^/]+\/read\/?$/.test(pathname)
  const isLocalDesktopLogin = isLocalDesktopProfile() && (pathname === "/login" || pathname === "/login/")
  if (pathname === researchHomeHref() || pathname === "/paper-agent" || pathname === "/paper-agent/" || isLocalDesktopLogin) return null
  return <ResearchCommandPalette showTrigger={!isPaperReader} />
}

export function Providers({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const [queryClient] = React.useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 60_000,
            refetchOnWindowFocus: false
          }
        }
      })
  )
  const isPaperAgentLab = pathname === "/paper-agent-lab" || pathname === "/paper-agent-lab/"

  if (isPaperAgentLab) {
    return (
      <QueryClientProvider client={queryClient}>
        <TooltipProvider delayDuration={180}>{children}</TooltipProvider>
      </QueryClientProvider>
    )
  }

  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={180}>
        <ThemeBridge />
        <PortalAccountProvider>
          <ResearchHistoryRuntime />
          <GlobalResearchCommands />
          {children}
        </PortalAccountProvider>
      </TooltipProvider>
    </QueryClientProvider>
  )
}
