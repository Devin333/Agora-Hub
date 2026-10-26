"use client"

import { PreferenceControls } from "@/components/layout/preference-controls"
import { getStudioEnvironment } from "@/features/studio/shared/lib/studio-navigation"
import { useI18n } from "@/lib/i18n/use-i18n"

export function StudioTopbar() {
  const { locale, t } = useI18n()
  const studioEnvironment = getStudioEnvironment(locale)
  const EnvironmentIcon = studioEnvironment.icon

  return (
    <header className="sticky top-0 z-20 border-b border-border bg-background/95 backdrop-blur">
      <div className="flex min-h-12 items-center justify-between gap-3 px-4 py-2 sm:px-6 lg:px-8">
        <div className="min-w-0">
          <p className="truncate text-xs font-semibold uppercase tracking-normal text-muted-foreground">
            {t("studio.operations")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <PreferenceControls className="hidden sm:flex" />
          <PreferenceControls className="sm:hidden" compact />
          <div className="flex items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground">
            <EnvironmentIcon className="size-4 text-link" />
            <span className="font-medium text-foreground">{studioEnvironment.label}</span>
            <span className="hidden sm:inline">{studioEnvironment.status}</span>
          </div>
        </div>
      </div>
    </header>
  )
}
