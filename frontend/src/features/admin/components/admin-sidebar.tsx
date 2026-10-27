"use client"

import {
  type LucideIcon,
  BarChart3,
  Bot,
  CheckCircle2,
  Database,
  FileEdit,
  GitBranch,
  Layers3,
  Radio,
  Send,
  Settings,
  ShieldCheck
} from "lucide-react"
import { BrandMark } from "@/components/brand/brand-mark"
import { adminNavItems } from "@/features/admin/lib/mock-data"
import { pick, ui } from "@/features/admin/lib/i18n"
import type { AdminLang, AdminPage } from "@/features/admin/types"
import { cn } from "@/lib/utils"

const pageIcons = {
  overview: BarChart3,
  ingestion: Database,
  pipeline: GitBranch,
  review: ShieldCheck,
  content: FileEdit,
  topics: Layers3,
  sources: Radio,
  agents: Bot,
  gates: CheckCircle2,
  publishing: Send,
  settings: Settings
} satisfies Record<AdminPage, LucideIcon>

export function AdminSidebar({
  activePage,
  lang,
  onPageChange
}: {
  activePage: AdminPage
  lang: AdminLang
  onPageChange: (page: AdminPage) => void
}) {
  return (
    <aside className="border-b border-sidebar-border bg-sidebar text-sidebar-foreground lg:fixed lg:inset-y-0 lg:left-0 lg:z-30 lg:w-[264px] lg:border-b-0 lg:border-r">
      <div className="flex h-full flex-col">
        <div className="flex items-center gap-3 px-[18px] pb-3 pt-4">
          <BrandMark size={32} />
          <div className="min-w-0">
            <p className="truncate text-[15px] font-semibold text-foreground">Agora Hub</p>
            <p className="truncate text-label text-muted-foreground">{ui[lang].console}</p>
          </div>
        </div>

        <nav
          className="rk-scroll flex gap-1 overflow-x-auto px-2.5 pb-3 lg:block lg:min-h-0 lg:flex-1 lg:overflow-y-auto"
          aria-label={ui[lang].admin}
        >
          <p className="hidden px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground lg:block">
            {ui[lang].admin}
          </p>
          <div className="flex gap-1 lg:mt-0.5 lg:block lg:space-y-0.5">
            {adminNavItems.map((item) => {
              const Icon = pageIcons[item.id]
              const active = activePage === item.id

              return (
                <button
                  key={item.id}
                  type="button"
                  className={`${cn(
                    "flex min-w-[12rem] items-center gap-3 rounded-xl px-2.5 py-[9px] text-left text-sidebar-foreground/80 outline-none transition-colors hover:bg-sidebar-accent/60 hover:text-sidebar-foreground focus-visible:ring-3 focus-visible:ring-ring/50 lg:w-full lg:min-w-0",
                    active && "bg-sidebar-accent text-sidebar-accent-foreground hover:bg-sidebar-accent"
                  )} text-body2`}
                  aria-current={active ? "page" : undefined}
                  onClick={() => onPageChange(item.id)}
                >
                  <Icon size={16} strokeWidth={1.8} className="shrink-0" aria-hidden="true" />
                  <span className="min-w-0">
                    <span className={cn("block truncate", active && "font-medium")}>{pick(item.label, lang)}</span>
                    <span className="block truncate text-label text-faint-foreground">{pick(item.purpose, lang)}</span>
                  </span>
                </button>
              )
            })}
          </div>
        </nav>
      </div>
    </aside>
  )
}
