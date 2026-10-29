import { Columns3, LayoutList, Table2, type LucideIcon } from "lucide-react"
import { cn } from "@/lib/utils"
import type { NewsViewMode } from "@/types/news"

const modes: Array<{ value: NewsViewMode; label: string; icon: LucideIcon }> = [
  { value: "card", label: "Cards", icon: LayoutList },
  { value: "dense", label: "Dense", icon: Columns3 },
  { value: "table", label: "Table", icon: Table2 },
]

/** Rakazo segmented view toggle: bordered group of compact pressed-state buttons. */
export function NewsViewModeToggle({
  value,
  onChange,
}: {
  value: NewsViewMode
  onChange: (value: NewsViewMode) => void
}) {
  return (
    <div role="group" aria-label="View mode" className="flex rounded-lg border border-border p-0.5">
      {modes.map((mode) => {
        const Icon = mode.icon
        const active = value === mode.value
        return (
          <button
            key={mode.value}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(mode.value)}
            className={cn(
              "flex h-7 items-center gap-1.5 rounded-md px-2 text-[13px] text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50",
              active && "bg-accent text-accent-foreground"
            )}
          >
            <Icon size={15} strokeWidth={1.9} aria-hidden="true" />
            <span className="hidden sm:inline">{mode.label}</span>
          </button>
        )
      })}
    </div>
  )
}
