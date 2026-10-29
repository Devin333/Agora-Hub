import * as React from "react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

type FilterChipProps = Omit<React.ComponentProps<typeof Button>, "variant" | "size"> & {
  active: boolean
}

/**
 * Rakazo filter chip: an inverted pill when selected, outlined otherwise.
 * `aria-pressed` mirrors the selection so assistive tech reads it as a toggle.
 */
export function FilterChip({ active, className, children, ...props }: FilterChipProps) {
  return (
    <Button
      type="button"
      variant={active ? "default" : "outline"}
      size="sm"
      aria-pressed={active}
      className={cn("rounded-full px-3 text-[12.5px]", !active && "text-muted-foreground", className)}
      {...props}
    >
      {children}
    </Button>
  )
}

/** Small removable chip that summarizes an applied filter (rakazo ActivityList). */
export function AppliedFilterChip({ label, onClear, clearLabel }: { label: string; onClear: () => void; clearLabel?: string }) {
  return (
    <button
      type="button"
      onClick={onClear}
      aria-label={clearLabel ?? `Clear filter ${label}`}
      className="inline-flex max-w-full items-center gap-1 rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[11.5px] text-foreground outline-none hover:bg-accent focus-visible:ring-3 focus-visible:ring-ring/50"
    >
      <span className="truncate">{label}</span>
      <span aria-hidden="true">×</span>
    </button>
  )
}
