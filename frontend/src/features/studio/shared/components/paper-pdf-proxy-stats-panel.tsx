"use client"

import type { ReactNode } from "react"
import { useQuery } from "@tanstack/react-query"
import { AlertTriangle, Ban, Clock3, FileText, Gauge } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { ApiRequestError } from "@/lib/api/client"
import { formatDataState } from "@/lib/i18n"
import { useI18n } from "@/lib/i18n/use-i18n"
import { queryKeys } from "@/lib/query/query-keys"
import { fetchPaperPdfProxyStats } from "@/features/studio/shared/api/pdf-proxy-stats-api"
import {
  StudioEmptyBlock,
  StudioMetricCard,
  StudioMetricGrid,
  StudioNotice,
  StudioPanel,
  type StudioIcon
} from "@/features/studio/shared/components/studio-dashboard"
import type { PaperPdfProxyStats } from "@/types/studio"

const PROXY_LIST_CLASS = "divide-y divide-border overflow-hidden rounded-xl border border-border"

export function PaperPdfProxyStatsPanel({ windowHours = 24 }: { windowHours?: number }) {
  const { locale, t, dateTime } = useI18n()
  const { data, error, isError, isLoading } = useQuery({
    queryKey: queryKeys.studio.pdfProxyStats(windowHours),
    queryFn: () => fetchPaperPdfProxyStats(windowHours),
    retry: false,
    staleTime: 30_000
  })

  if (isLoading) {
    return (
      <StudioPanel title={t("studio.pdfProxy.title")} description={t("studio.pdfProxy.loading")}>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3].map((item) => (
            <div key={item} className="h-24 animate-pulse rounded-2xl bg-muted" />
          ))}
        </div>
      </StudioPanel>
    )
  }

  if (isError) {
    return (
      <StudioNotice tone="danger" title={t("studio.pdfProxy.errorTitle")}>
        <p>{error instanceof Error ? error.message : t("studio.pdfProxy.errorMessage")}</p>
        {error instanceof ApiRequestError && error.requestId ? (
          <p className="mt-1 font-mono text-label">requestId={error.requestId}</p>
        ) : null}
      </StudioNotice>
    )
  }

  if (!data || data.dataState === "empty") {
    return (
      <StudioPanel title={t("studio.pdfProxy.title")} description={t("studio.pdfProxy.window", { hours: windowHours })}>
        <StudioEmptyBlock
          title={t("studio.pdfProxy.emptyTitle")}
          description={t("studio.pdfProxy.emptyDescription")}
        />
      </StudioPanel>
    )
  }

  return (
    <StudioPanel
      title={t("studio.pdfProxy.title")}
      description={t("studio.pdfProxy.windowEnding", { hours: data.windowHours, time: dateTime(data.windowEndedAt) })}
      actions={<Badge variant={data.dataState === "ready" ? "success" : "warning"}>{formatDataState(locale, data.dataState)}</Badge>}
    >
      <PdfProxyStatsContent stats={data} />
    </StudioPanel>
  )
}

function PdfProxyStatsContent({ stats }: { stats: PaperPdfProxyStats }) {
  const { t, dateTime } = useI18n()
  const topError = Object.entries(stats.errorsByCode).sort(([, left], [, right]) => right - left)[0]
  const topHost = stats.topHosts[0]
  return (
    <div className="space-y-3">
      {stats.dataState === "partial" || stats.notices.length ? (
        <StudioNotice tone={stats.dataState === "partial" ? "warning" : "info"} title={t("studio.pdfProxy.notice")}>
          <div className="space-y-1">
            {stats.notices.map((notice) => (
              <p key={notice}>{notice}</p>
            ))}
          </div>
        </StudioNotice>
      ) : null}

      <StudioMetricGrid className="xl:grid-cols-4 2xl:grid-cols-4">
        <StudioMetricCard label={t("studio.pdfProxy.requests")} value={stats.totalRequests} detail={t("studio.pdfProxy.streamed", { count: stats.successCount })} icon={FileText} tone="accent" />
        <StudioMetricCard label={t("studio.pdfProxy.errors")} value={stats.errorCount} detail={topError ? `${topError[0]} (${topError[1]})` : t("studio.pdfProxy.noErrors")} icon={AlertTriangle} tone={stats.errorCount ? "warning" : "success"} />
        <StudioMetricCard label={t("studio.pdfProxy.timeouts")} value={stats.timeoutCount} detail={t("studio.pdfProxy.oversized", { count: stats.oversizedCount })} icon={Clock3} tone={stats.timeoutCount ? "danger" : "neutral"} />
        <StudioMetricCard label={t("studio.pdfProxy.blocked")} value={stats.blockedCount} detail={topHost ? topHost.host : t("studio.pdfProxy.noHostData")} icon={Ban} tone={stats.blockedCount ? "danger" : "neutral"} />
      </StudioMetricGrid>

      <section className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <ProxyListSection icon={Gauge} title={t("studio.pdfProxy.topHosts")}>
          {stats.topHosts.length ? (
            <ul className={PROXY_LIST_CLASS}>
              {stats.topHosts.map((host) => (
                <li key={host.host} className="flex items-center justify-between gap-3 px-4 py-2.5">
                  <span className="truncate text-[13.5px] font-medium text-foreground">{host.host}</span>
                  <span className="shrink-0 text-label tabular-nums text-muted-foreground">
                    {t("studio.pdfProxy.hostRow", { requests: host.requestCount, errors: host.errorCount })}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-body2 text-muted-foreground">{t("studio.pdfProxy.noHostWindow")}</p>
          )}
        </ProxyListSection>

        <ProxyListSection icon={AlertTriangle} title={t("studio.pdfProxy.recentErrors")}>
          {stats.recentErrors.length ? (
            <ul className={PROXY_LIST_CLASS}>
              {stats.recentErrors.map((item) => (
                <li key={`${item.timestamp}-${item.code}-${item.path}`} className="px-4 py-2.5 text-label">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono font-semibold text-warning-text">{item.code}</span>
                    <span className="tabular-nums text-muted-foreground">{dateTime(item.timestamp)}</span>
                  </div>
                  <p className="mt-0.5 truncate text-muted-foreground">{[item.host, item.path].filter(Boolean).join("")}</p>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-body2 text-muted-foreground">{t("studio.pdfProxy.noRecentErrors")}</p>
          )}
        </ProxyListSection>
      </section>
    </div>
  )
}

/** Unframed sub-section inside the proxy panel: icon + 13.5px title above a divided list. */
function ProxyListSection({ icon: Icon, title, children }: { icon: StudioIcon; title: string; children: ReactNode }) {
  return (
    <section className="min-w-0 space-y-2">
      <div className="flex items-center gap-2">
        <Icon size={16} strokeWidth={1.8} aria-hidden="true" className="text-muted-foreground" />
        <h3 className="text-[13.5px] font-semibold text-foreground">{title}</h3>
      </div>
      {children}
    </section>
  )
}
