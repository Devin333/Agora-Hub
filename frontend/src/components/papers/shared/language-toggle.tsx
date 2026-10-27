"use client"

import { Languages } from "lucide-react"
import { Button } from "@/components/ui/button"
import { papersCopy, t } from "@/lib/papers/copy"
import type { Locale } from "@/lib/papers/types"
import { cn } from "@/lib/utils"

export function PapersLanguageToggle({
  locale,
  onLocaleChange
}: {
  locale: Locale
  onLocaleChange: (locale: Locale) => void
}) {
  const nextLocale = locale === "zh" ? "en" : "zh"

  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="gap-2 rounded-full px-2"
      title={t(papersCopy.languageToggle, locale)}
      onClick={() => onLocaleChange(nextLocale)}
    >
      <Languages className="size-4" strokeWidth={1.8} aria-hidden="true" />
      <span className="flex items-center gap-0.5 text-label font-semibold">
        <span
          className={cn(
            "rounded-full px-2 py-0.5 transition-colors",
            locale === "zh" ? "bg-primary text-primary-foreground" : "text-muted-foreground"
          )}
        >
          中
        </span>
        <span
          className={cn(
            "rounded-full px-2 py-0.5 transition-colors",
            locale === "en" ? "bg-primary text-primary-foreground" : "text-muted-foreground"
          )}
        >
          EN
        </span>
      </span>
    </Button>
  )
}
