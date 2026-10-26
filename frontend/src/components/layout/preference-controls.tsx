"use client"

import { Languages } from "lucide-react"
import { AppearanceMenu } from "@/components/theme/appearance-menu"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useI18n } from "@/lib/i18n/use-i18n"
import { cn } from "@/lib/utils"
import { useUiStore } from "@/stores/ui-store"

type PreferenceControlsProps = {
  className?: string
  compact?: boolean
}

export function PreferenceControls({ className, compact = false }: PreferenceControlsProps) {
  const { locale, t } = useI18n()
  const setLocale = useUiStore((state) => state.setLocale)
  const nextLocale = locale === "zh" ? "en" : "zh"
  const localeLabel = t("preference.language.switch")

  return (
    <div className={cn("flex items-center gap-1", className)}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size={compact ? "icon" : "sm"}
            aria-label={localeLabel}
            onClick={() => setLocale(nextLocale)}
          >
            <Languages />
            {!compact ? <span>{t("preference.language.current")}</span> : null}
          </Button>
        </TooltipTrigger>
        <TooltipContent>{localeLabel}</TooltipContent>
      </Tooltip>
      <AppearanceMenu showLabel={!compact} />
    </div>
  )
}
