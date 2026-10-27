"use client"

import { Filter, Play } from "lucide-react"
import { SearchField } from "@/components/common/search-field"
import { Button } from "@/components/ui/button"
import { PreferenceControls } from "@/components/layout/preference-controls"
import { pageTitleKey, ui } from "@/features/admin/lib/i18n"
import type { AdminLang, AdminPage } from "@/features/admin/types"

export function AdminHeader({
  activePage,
  lang
}: {
  activePage: AdminPage
  lang: AdminLang
}) {
  const copy = ui[lang]
  const pageTitle = copy[pageTitleKey[activePage]]

  return (
    <header className="sticky top-0 z-20 border-b border-border bg-background">
      <div className="flex min-h-14 flex-col gap-3 px-4 py-3 md:flex-row md:items-center md:justify-between md:px-6">
        <div className="min-w-0">
          <p className="text-caption font-medium uppercase tracking-wider text-faint-foreground">
            {copy.admin} / {pageTitle}
          </p>
          <h1 className="mt-0.5 truncate text-heading3 font-semibold text-foreground">{pageTitle}</h1>
        </div>

        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2 md:justify-end">
          <SearchField
            className="min-w-[14rem] flex-1 md:max-w-[320px]"
            placeholder={copy.searchPlaceholder}
            aria-label={copy.searchPlaceholder}
          />
          <Button type="button" variant="outline" size="sm">
            <Filter strokeWidth={1.8} aria-hidden="true" />
            {copy.filters}
          </Button>
          <PreferenceControls compact />
          <Button type="button" size="sm">
            <Play strokeWidth={1.8} aria-hidden="true" />
            {copy.runPipeline}
          </Button>
        </div>
      </div>
    </header>
  )
}
