import type { Metadata, Viewport } from "next"
import "@/app/globals.css"
import "@/components/papers/open-reader/open-reader.module.css"
import "@/components/papers/paper-reader/paper-document-reader.module.css"
import "katex/dist/katex.min.css"
import "reactflow/dist/style.css"
import { AppShell } from "@/components/layout/app-shell"
import { Providers } from "@/components/providers"
import { getFrontendSurface } from "@/lib/frontend-surface"
import { THEME_COLORS } from "@/lib/theme/appearance"

export const metadata: Metadata = {
  title: "Agora Hub 情报工作台",
  description: "新闻、论文与项目的一体化情报、证据质量和智能体运行工作台。"
}

export const viewport: Viewport = {
  themeColor: THEME_COLORS.dark
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const surface = getFrontendSurface()

  // theme-init.js sets data-theme before first paint, so the server markup cannot match it.
  return (
    <html lang="zh-CN" className="h-full" data-newsroom-surface={surface} suppressHydrationWarning>
      <head>
        {/* eslint-disable-next-line @next/next/no-sync-scripts -- must run before first paint */}
        <script src="/theme-init.js" />
      </head>
      <body className="min-h-full bg-background font-sans text-foreground">
        <Providers>
          <AppShell surface={surface}>{children}</AppShell>
        </Providers>
      </body>
    </html>
  )
}
