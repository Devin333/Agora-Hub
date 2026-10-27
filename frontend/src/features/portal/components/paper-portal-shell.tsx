"use client"

import type { ReactNode } from "react"
import Link from "next/link"
import { PortalAccountControl } from "@/components/auth/portal-account-control"
import { BrandMark } from "@/components/brand/brand-mark"
import { AppearanceMenu } from "@/components/theme/appearance-menu"
import type { Locale } from "@/lib/papers/types"
import { researchHomeHref } from "@/lib/research/routes"
import { PaperPortalHeader } from "./paper-portal-header"

/**
 * Rakazo app frame shared by the paper portal pages: a 316px sidebar on the
 * left and a main pane whose content scrolls under floating header pills.
 * `overlay` renders inside the main pane above the scroll column (trays, drawers).
 */
export function PaperPortalShell({ locale, sidebarLabel, sidebar, overlay, children }: {
  locale: Locale
  sidebarLabel: string
  sidebar: ReactNode
  overlay?: ReactNode
  children: ReactNode
}) {
  return <div className="flex h-dvh overflow-hidden bg-background text-foreground">
    <aside aria-label={sidebarLabel} className="flex w-[316px] shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground max-lg:w-[272px]">
      <div className="flex items-center gap-2 px-[18px] pb-3 pt-4">
        <Link href={researchHomeHref()} aria-label="Agora AI" className="flex min-w-0 items-center gap-2.5 rounded-lg text-body1 font-semibold outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
          <BrandMark size={28} />
          <span className="truncate">Agora AI</span>
        </Link>
      </div>
      <div className="rk-scroll min-h-0 flex-1 overflow-y-auto px-2.5 pb-4">{sidebar}</div>
      <div className="flex items-center justify-between gap-2 border-t border-sidebar-border px-2.5 py-3">
        <PortalAccountControl side="top" />
        <AppearanceMenu align="start" className="size-7 rounded-full" />
      </div>
    </aside>
    <div className="relative flex min-w-0 flex-1 flex-col">
      <PaperPortalHeader locale={locale} />
      <div className="rk-scroll min-h-0 flex-1 overflow-y-auto pt-[68px]">{children}</div>
      {overlay}
    </div>
  </div>
}

/** Rakazo sidebar section header (uppercase caption). */
export const sidebarSectionHeaderClassName = "px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-faint-foreground"

/** Rakazo sidebar row; selection is driven by `aria-pressed` / `aria-current`. */
export const sidebarRowClassName = "flex w-full items-center gap-3 rounded-xl px-2.5 py-[10px] text-left text-body2 font-medium text-sidebar-foreground outline-none transition-colors hover:bg-sidebar-accent/60 focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:text-faint-foreground disabled:hover:bg-transparent aria-pressed:bg-sidebar-accent aria-pressed:text-sidebar-accent-foreground"

/** Count / meta text placed at the end of a sidebar row. */
export const sidebarRowMetaClassName = "ml-auto shrink-0 text-label font-normal tabular-nums text-faint-foreground"

/** Native select styled like the rakazo outline control (kept native for keyboard + form semantics). */
export const nativeSelectClassName = "h-7 cursor-pointer rounded-lg border border-border bg-transparent px-2 text-label text-foreground outline-none transition-colors hover:bg-accent focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 [&>option]:bg-popover [&>option]:text-popover-foreground"

/** Rakazo search field: bordered card box with a leading icon. */
export const searchBoxClassName = "flex h-11 items-center gap-2 rounded-xl border border-border bg-card pe-1.5 ps-3 text-muted-foreground transition-colors focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50 dark:bg-input"

/** Removable filter chip. */
export const filterChipClassName = "inline-flex h-6 max-w-full items-center gap-1 rounded-full bg-secondary px-2.5 text-label font-medium text-secondary-foreground outline-none transition-colors hover:bg-accent focus-visible:ring-3 focus-visible:ring-ring/50 [&>span]:truncate [&>svg]:shrink-0"
