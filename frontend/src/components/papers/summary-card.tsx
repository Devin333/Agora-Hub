import { cn } from "@/lib/utils"

/** Stat tile: muted label over a tabular value, on the card surface. */
export function SummaryCard({ label, value, compact = false }: { label: string; value: string | number; compact?: boolean }) {
  return (
    <div
      aria-label={`${label}: ${value}`}
      className={cn(
        "flex flex-col rounded-xl border bg-card",
        compact ? "min-h-[4.25rem] justify-between px-3 py-2.5 text-left" : "p-4"
      )}
    >
      <p className="text-label text-muted-foreground">{label}</p>
      <p className={cn("mt-1 font-semibold tabular-nums text-foreground", compact ? "text-heading3" : "text-heading2")}>
        {value}
      </p>
    </div>
  )
}
