import { cn } from "@/lib/utils"

export type ScoreMeterProps = {
  value: number
  label?: string
  max?: number
}

export function ScoreMeter({ value, label, max = 100 }: ScoreMeterProps) {
  const normalized = Math.max(0, Math.min(100, (value / max) * 100))
  const tone = normalized >= 85 ? "bg-success" : normalized >= 70 ? "bg-warning" : "bg-destructive"

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-3 text-label text-muted-foreground">
        <span>{label ?? "分数"}</span>
        <span className="font-medium tabular-nums text-foreground">
          {Math.round(value)}/{max}
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div className={cn("h-full rounded-full", tone)} style={{ width: `${normalized}%` }} />
      </div>
    </div>
  )
}
