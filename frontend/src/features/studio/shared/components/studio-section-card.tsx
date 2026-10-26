"use client"

import Link from "next/link"
import { ArrowRight } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import {
  studioStatusLabel,
  studioStatusTone
} from "@/features/studio/shared/lib/studio-navigation"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { StudioModuleEntry } from "@/types/studio"

export function StudioSectionCard({ entry }: { entry: StudioModuleEntry }) {
  const { locale } = useI18n()
  const Icon = entry.icon

  return (
    <Link
      href={entry.href}
      className="group grid min-h-[190px] grid-rows-[auto_1fr_auto] rounded-md border border-border bg-card p-4 shadow-sm transition-colors hover:border-link/60 hover:bg-secondary/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-center gap-3">
          {Icon ? (
            <span className="flex size-10 shrink-0 items-center justify-center rounded-md border border-border bg-secondary text-link">
              <Icon className="size-5" />
            </span>
          ) : null}
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold text-foreground">{entry.title}</h2>
            <p className="mt-1 text-xs text-muted-foreground">{entry.coreObject}</p>
          </div>
        </div>
        <Badge variant={studioStatusTone(entry.status)}>{studioStatusLabel(entry.status, locale)}</Badge>
      </div>

      <div className="mt-4 space-y-3">
        <p className="text-sm leading-6 text-muted-foreground">{entry.description}</p>
        <div>
          <p className="text-xs font-semibold uppercase tracking-normal text-muted-foreground">{locale === "zh" ? "目标 API" : "Target API"}</p>
          <p className="mt-1 break-all font-mono text-xs text-foreground">{entry.targetApi}</p>
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between gap-3 border-t border-border pt-3 text-sm font-medium text-link">
        <span>{entry.actionLabel ?? (locale === "zh" ? "打开模块" : "Open module")}</span>
        <ArrowRight className="size-4 transition-transform group-hover:translate-x-0.5" />
      </div>
    </Link>
  )
}
