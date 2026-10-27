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
      className="group flex min-h-[190px] flex-col gap-4 rounded-2xl border border-border bg-card p-4 text-foreground outline-none transition-colors hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-ring/50"
    >
      <div className="flex items-start justify-between gap-3">
        {Icon ? (
          <span className="grid size-9 shrink-0 place-items-center rounded-[10px] bg-accent text-accent-foreground">
            <Icon size={16} strokeWidth={1.8} aria-hidden="true" />
          </span>
        ) : null}
        <Badge variant={studioStatusTone(entry.status)}>{studioStatusLabel(entry.status, locale)}</Badge>
      </div>

      <div className="min-w-0 space-y-1">
        <h2 className="truncate text-[15px] font-semibold">{entry.title}</h2>
        <p className="text-label text-muted-foreground">{entry.coreObject}</p>
        <p className="line-clamp-3 pt-1 text-[13px] leading-relaxed text-muted-foreground">{entry.description}</p>
      </div>

      <div className="mt-auto space-y-3">
        <div>
          <p className="text-caption font-semibold uppercase tracking-wider text-faint-foreground">{locale === "zh" ? "目标 API" : "Target API"}</p>
          <p className="mt-0.5 break-all font-mono text-label text-foreground/80">{entry.targetApi}</p>
        </div>
        <div className="flex items-center justify-between gap-3 text-body2 font-medium text-link">
          <span>{entry.actionLabel ?? (locale === "zh" ? "打开模块" : "Open module")}</span>
          <ArrowRight size={15} strokeWidth={1.8} className="transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
        </div>
      </div>
    </Link>
  )
}
