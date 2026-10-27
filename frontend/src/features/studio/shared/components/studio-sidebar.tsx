"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import { BrandMark } from "@/components/brand/brand-mark"
import { PreferenceControls } from "@/components/layout/preference-controls"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import {
  getLocalizedStudioNavigationGroups,
  getStudioEnvironment,
  studioStatusLabel,
  studioStatusTone
} from "@/features/studio/shared/lib/studio-navigation"
import { useI18n } from "@/lib/i18n/use-i18n"

/** Studio navigation laid out like the rakazo sidebar: brand header, caption sections, rounded rows. */
export function StudioSidebar() {
  const pathname = usePathname()
  const { locale, t } = useI18n()
  const navigationGroups = getLocalizedStudioNavigationGroups(locale)
  const environment = getStudioEnvironment(locale)
  const EnvironmentIcon = environment.icon

  return (
    <aside className="sticky top-0 hidden h-screen w-[264px] shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground lg:flex">
      <Link
        href="/studio"
        className="flex items-center gap-3 rounded-xl px-[18px] pb-3 pt-4 outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        <BrandMark size={32} />
        <span className="min-w-0">
          <span className="block truncate text-[15px] font-semibold text-foreground">{t("studio.title")}</span>
          <span className="block truncate text-label text-muted-foreground">{t("studio.subtitle")}</span>
        </span>
      </Link>

      <nav className="rk-scroll min-h-0 flex-1 space-y-4 overflow-y-auto px-2.5 pb-3" aria-label={t("studio.title")}>
        {navigationGroups.map((group) => (
          <div key={group.label}>
            <p className="px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">
              {group.label}
            </p>
            <div className="mt-0.5 space-y-0.5">
              {group.items.map((item) => {
                const Icon = item.icon
                const active = item.href === "/studio" ? pathname === item.href : pathname.startsWith(item.href)

                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    aria-current={active ? "page" : undefined}
                    className={`${cn(
                      "flex items-center gap-3 rounded-xl px-2.5 py-[9px] text-sidebar-foreground/80 outline-none transition-colors hover:bg-sidebar-accent/60 hover:text-sidebar-foreground focus-visible:ring-3 focus-visible:ring-ring/50",
                      active && "bg-sidebar-accent font-medium text-sidebar-accent-foreground hover:bg-sidebar-accent"
                    )} text-body2`}
                  >
                    {Icon ? <Icon size={16} strokeWidth={1.8} className="shrink-0" aria-hidden="true" /> : null}
                    <span className="min-w-0 flex-1 truncate">{item.label}</span>
                    {item.status ? (
                      <Badge className="h-[18px] px-1.5 text-[10px]" variant={studioStatusTone(item.status)}>
                        {studioStatusLabel(item.status, locale)}
                      </Badge>
                    ) : null}
                  </Link>
                )
              })}
            </div>
          </div>
        ))}
      </nav>

      <div className="space-y-2 border-t border-sidebar-border px-2.5 py-3">
        <div className="flex items-center gap-3 px-2.5">
          <span className="grid size-9 shrink-0 place-items-center rounded-full bg-muted text-muted-foreground">
            <EnvironmentIcon size={16} strokeWidth={1.8} aria-hidden="true" />
          </span>
          <span className="min-w-0">
            <span className="block truncate text-body2 font-medium text-foreground">{environment.label}</span>
            <span className="block truncate text-label text-faint-foreground">{environment.status}</span>
          </span>
        </div>
        <PreferenceControls className="px-1" />
      </div>
    </aside>
  )
}
