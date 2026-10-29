"use client"

import { Monitor, Moon, Sun, type LucideIcon } from "lucide-react"
import { Button, type ButtonProps } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger
} from "@/components/ui/dropdown-menu"
import { useI18n } from "@/lib/i18n/use-i18n"
import { APPEARANCE_PREFERENCES, isAppearancePreference, type AppearancePreference } from "@/lib/theme/appearance"
import { useUiStore } from "@/stores/ui-store"

const appearanceOptions: Record<AppearancePreference, { icon: LucideIcon; labelKey: string }> = {
  system: { icon: Monitor, labelKey: "preference.theme.system" },
  light: { icon: Sun, labelKey: "preference.theme.light" },
  dark: { icon: Moon, labelKey: "preference.theme.dark" }
}

/** Reads and writes the appearance preference; shared by every header that offers it. */
export function useAppearanceOptions() {
  const { t } = useI18n()
  const appearance = useUiStore((state) => state.appearance)
  const setAppearance = useUiStore((state) => state.setAppearance)
  const options = APPEARANCE_PREFERENCES.map((value) => ({ value, icon: appearanceOptions[value].icon, label: t(appearanceOptions[value].labelKey) }))
  return { appearance, setAppearance, options, title: t("preference.theme.choose") }
}

/** Header control: an icon (or labelled) button that opens the system / light / dark choice. */
export function AppearanceMenu({
  showLabel = false,
  variant = "ghost",
  size,
  align = "end",
  className
}: {
  showLabel?: boolean
  variant?: ButtonProps["variant"]
  size?: ButtonProps["size"]
  align?: "start" | "center" | "end"
  className?: string
}) {
  const { appearance, setAppearance, options, title } = useAppearanceOptions()
  const current = options.find((option) => option.value === appearance) ?? options[0]
  const Icon = current.icon

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant={variant}
          size={size ?? (showLabel ? "sm" : "icon")}
          className={className}
          aria-label={`${title}：${current.label}`}
          title={title}
        >
          <Icon />
          {showLabel ? <span>{current.label}</span> : null}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align={align} className="min-w-40">
        <DropdownMenuLabel>{title}</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={appearance} onValueChange={(value) => isAppearancePreference(value) && setAppearance(value)}>
          {options.map((option) => (
            <DropdownMenuRadioItem key={option.value} value={option.value}>
              <option.icon />
              {option.label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
