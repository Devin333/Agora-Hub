import { Badge } from "@/components/ui/badge"
import { useI18n } from "@/lib/i18n/use-i18n"
import { cn } from "@/lib/utils"
import type { ReviewRiskLevel } from "@/types/review"

const riskConfig: Record<ReviewRiskLevel, { zh: string; en: string; className: string }> = {
  low: {
    zh: "低",
    en: "Low",
    className: "border-success/30 bg-success/10 text-success"
  },
  medium: {
    zh: "中",
    en: "Medium",
    className: "border-link/30 bg-link/10 text-link"
  },
  high: {
    zh: "高",
    en: "High",
    className: "border-warning/50 bg-warning/15 text-warning"
  },
  critical: {
    zh: "严重",
    en: "Critical",
    className: "border-destructive bg-destructive/15 text-destructive shadow-sm"
  }
}

export function ReviewRiskBadge({ riskLevel }: { riskLevel: ReviewRiskLevel }) {
  const { locale } = useI18n()
  const config = riskConfig[riskLevel]
  return (
    <Badge className={cn("uppercase tracking-normal", config.className)} variant="default">
      {config[locale]}
    </Badge>
  )
}
