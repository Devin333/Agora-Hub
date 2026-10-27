"use client"

import Link from "next/link"
import { BrandMark } from "@/components/brand/brand-mark"
import { PreferenceControls } from "@/components/layout/preference-controls"
import { getStudioEnvironment } from "@/features/studio/shared/lib/studio-navigation"
import { useI18n } from "@/lib/i18n/use-i18n"

/**
 * Compact bar for narrow viewports, where the sidebar (which carries the brand,
 * environment and preferences on desktop) is hidden.
 */
export function StudioTopbar() {
  const { locale, t } = useI18n()
  const studioEnvironment = getStudioEnvironment(locale)

  return (
    <header className="sticky top-0 z-20 border-b border-border bg-background/90 backdrop-blur lg:hidden">
      <div className="flex h-12 items-center justify-between gap-3 px-4">
        <Link href="/studio" className="flex min-w-0 items-center gap-2.5 rounded-lg outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
          <BrandMark size={24} />
          <span className="min-w-0">
            <span className="block truncate text-body2 font-semibold text-foreground">{t("studio.title")}</span>
            <span className="sr-only">{studioEnvironment.label}</span>
          </span>
        </Link>
        <PreferenceControls compact />
      </div>
    </header>
  )
}
