"use client"

import * as React from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { TooltipProvider } from "@/components/ui/tooltip"
import { useUiStore } from "@/stores/ui-store"
import { ResearchHistoryTracker } from "@/components/research/research-history-tracker"

function ThemeBridge() {
  const theme = useUiStore((state) => state.theme)
  const locale = useUiStore((state) => state.locale)

  React.useEffect(() => {
    const root = document.documentElement
    root.classList.toggle("dark", theme === "dark")
    root.dataset.theme = theme
  }, [theme])

  React.useEffect(() => {
    document.documentElement.lang = locale === "zh" ? "zh-CN" : "en"
  }, [locale])

  return null
}

export function Providers({ children }: { children: React.ReactNode }) {
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

  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={180}>
        <ThemeBridge />
        <React.Suspense fallback={null}><ResearchHistoryTracker /></React.Suspense>
        {children}
      </TooltipProvider>
    </QueryClientProvider>
  )
}
