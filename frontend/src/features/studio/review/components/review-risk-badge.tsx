import { Badge, type BadgeVariant } from "@/components/ui/badge"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { ReviewRiskLevel } from "@/types/review"

const riskConfig: Record<ReviewRiskLevel, { zh: string; en: string; variant: BadgeVariant; className?: string }> = {
  low: { zh: "低", en: "Low", variant: "success" },
  medium: { zh: "中", en: "Medium", variant: "info" },
  high: { zh: "高", en: "High", variant: "warning" },
  critical: { zh: "严重", en: "Critical", variant: "danger", className: "border-destructive/40" }
}

/** Risk pill: tinted rakazo status badge; critical keeps an outline so it stands out in dense tables. */
export function ReviewRiskBadge({ riskLevel }: { riskLevel: ReviewRiskLevel }) {
  const { locale } = useI18n()
  const config = riskConfig[riskLevel]
  return (
    <Badge className={config.className} variant={config.variant}>
      {config[locale]}
    </Badge>
  )
}
