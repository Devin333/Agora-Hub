"use client"

import Link from "next/link"
import { WandSparkles } from "lucide-react"
import { Button } from "@/components/ui/button"
import type { Locale } from "@/lib/papers/types"
import { researchHomeHref, researchPapersHref } from "@/lib/research/routes"

/** Floating rakazo pills over the main pane: section navigation and the primary entry back into research. */
export function PaperPortalHeader({ locale }: { locale: Locale }) {
  const zh = locale === "zh"
  const links = [
    { href: researchHomeHref(), label: zh ? "首页" : "Home" },
    { href: researchPapersHref(), label: zh ? "论文研究" : "Papers", current: true },
    { href: "/projects", label: zh ? "项目雷达" : "Projects" },
    { href: "/community", label: zh ? "社区信号" : "Community" }
  ]
  return <header className="pointer-events-none absolute inset-x-0 top-0 z-20 flex items-center justify-between gap-3 px-4 pt-4">
    <nav aria-label={zh ? "主导航" : "Main navigation"} className="pointer-events-auto flex h-9 items-center gap-0.5 rounded-full border border-border bg-muted/90 p-1 backdrop-blur">
      {links.map((link) => <Link
        key={link.label}
        href={link.href}
        aria-current={link.current ? "true" : undefined}
        className="inline-flex h-7 items-center rounded-full px-3 text-body2 font-medium text-foreground/75 outline-none transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 aria-[current]:bg-accent aria-[current]:text-foreground"
      >{link.label}</Link>)}
    </nav>
    <Button asChild variant="floating" size="lg" className="pointer-events-auto px-3.5">
      <Link href={`${researchHomeHref()}#workspace`}><WandSparkles size={15} strokeWidth={1.8} />{zh ? "开始研究" : "New research"}</Link>
    </Button>
  </header>
}
