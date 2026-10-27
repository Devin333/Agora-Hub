"use client"

import { Badge } from "@/components/ui/badge"
import { StudioPanel } from "@/features/studio/shared/components/studio-dashboard"
import { formatDateTime as formatLocalizedDateTime, formatStatus } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { ReviewHistoryEvent, StudioReviewItem } from "@/types/review"

export function ReviewHistoryPanel({
  history,
  items,
  title
}: {
  history?: ReviewHistoryEvent[]
  items?: StudioReviewItem[]
  title?: string
}) {
  const { locale, t } = useI18n()
  const events = history ?? eventsFromItems(items ?? [])

  return (
    <StudioPanel title={title ?? t("studio.review.operationHistory")} actions={<Badge variant="muted">{events.length}</Badge>}>
      {events.length ? (
        <ol className="divide-y divide-border overflow-hidden rounded-xl border border-border">
          {events.map((event) => (
            <li key={event.id} className="px-4 py-3">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-label text-muted-foreground">
                <Badge variant="default">{formatStatus(locale, event.type)}</Badge>
                {event.status ? <span>{formatStatus(locale, event.status)}</span> : null}
                {event.actor ? <span>{t("studio.review.byActor", { actor: event.actor })}</span> : null}
                {event.at ? <span className="tabular-nums">{formatLocalizedDateTime(locale, event.at)}</span> : null}
              </div>
              {event.reason ? <p className="mt-1.5 text-body2 text-muted-foreground">{event.reason}</p> : null}
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-body2 text-muted-foreground">{t("studio.review.noDecisions")}</p>
      )}
    </StudioPanel>
  )
}

function eventsFromItems(items: StudioReviewItem[]): ReviewHistoryEvent[] {
  return items
    .filter((item) => item.status !== "pending")
    .flatMap((item) =>
      item.history?.length
        ? item.history
        : [
            {
              id: `${item.approvalId}:status`,
              type: item.status,
              at: item.requestedAt,
              reason: item.reason,
              status: item.rawStatus ?? item.status
            }
          ]
    )
}
