import { Badge, type BadgeVariant } from "@/components/ui/badge"
import type { AdminLang, AdminStatus } from "@/features/admin/types"

const statusLabels: Record<AdminStatus, { zh: string; en: string }> = {
  ok: { zh: "正常", en: "OK" },
  warning: { zh: "警告", en: "Warning" },
  failed: { zh: "失败", en: "Failed" },
  review: { zh: "复核", en: "Review" },
  running: { zh: "运行中", en: "Running" },
  blocked: { zh: "已阻塞", en: "Blocked" }
}

const statusVariant: Record<AdminStatus, BadgeVariant> = {
  ok: "success",
  warning: "warning",
  failed: "danger",
  review: "info",
  running: "info",
  blocked: "danger"
}

/** Admin pipeline status mapped onto the rakazo status badge tones. */
export function StatusPill({ status, lang, className }: { status: AdminStatus; lang: AdminLang; className?: string }) {
  return (
    <Badge variant={statusVariant[status]} className={className}>
      {statusLabels[status][lang]}
    </Badge>
  )
}
