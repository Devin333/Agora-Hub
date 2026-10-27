"use client"

import { useState } from "react"
import Link from "next/link"
import { ArrowLeft, Play } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { StudioFallbackNotice } from "@/features/studio/shared/components/studio-fallback-notice"
import { StudioPageHeader, StudioPanel, studioFieldClass } from "@/features/studio/shared/components/studio-dashboard"
import { BoardCardGrid } from "@/features/studio/boards/components/board-card-grid"
import { BoardOutputPanel } from "@/features/studio/boards/components/board-output-panel"
import { CrossBoardInsightPanel } from "@/features/studio/boards/components/cross-board-insight-panel"
import { useBoardList } from "@/features/studio/boards/hooks/use-board-list"
import { useBoardOutput } from "@/features/studio/boards/hooks/use-board-output"
import { useI18n } from "@/lib/i18n/use-i18n"
import type {
  StudioBoardDetailViewModel,
  StudioBoardListViewModel
} from "@/types/board"

export function BoardCenterPage({ initialData }: { initialData: StudioBoardListViewModel }) {
  const { locale, t } = useI18n()
  const { data } = useBoardList(initialData)

  return (
    <main className="space-y-5">
      <StudioPageHeader
        eyebrow={locale === "zh" ? "业务" : "Business"}
        title={t("studio.module.boardCenter.title")}
        description={t("studio.module.boardCenter.description")}
      />
      <NoticeList notices={data.notices} />
      <BoardCardGrid summaries={data.summaries} definitions={data.definitions} />
    </main>
  )
}

export function BoardDetailPage({ detail }: { detail: StudioBoardDetailViewModel }) {
  const { locale, t } = useI18n()
  const { data: output, isLoading, buildOutput } = useBoardOutput(detail.summary.boardType, detail.output)
  const [itemsJson, setItemsJson] = useState(detail.sampleItemsJson)
  const [topic, setTopic] = useState("Agent Memory")
  const [formError, setFormError] = useState<string | undefined>()

  async function handleBuildOutput() {
    setFormError(undefined)
    let parsed: unknown
    try {
      parsed = JSON.parse(itemsJson)
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "Invalid JSON")
      return
    }

    if (!Array.isArray(parsed) || !parsed.every(isRecord)) {
      setFormError("Sample items must be a JSON array of objects.")
      return
    }

    await buildOutput({ items: parsed, topic: topic.trim() || undefined })
  }

  return (
    <main className="space-y-5">
      <StudioPageHeader
        eyebrow={locale === "zh" ? "业务板中心" : "Board Center"}
        title={detail.summary.title}
        description={detail.definition.description}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/studio/boards">
              <ArrowLeft size={15} strokeWidth={1.8} aria-hidden="true" />
              {locale === "zh" ? "业务板" : "Boards"}
            </Link>
          </Button>
        }
      />

      <NoticeList notices={[...detail.notices, ...detail.summary.notices, ...output.notices]} />

      <section className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_24rem]">
        <StudioPanel
          title={t("studio.boards.definition")}
          description={detail.summary.boardType}
          actions={<Badge variant={detail.summary.status === "ready" ? "success" : "warning"}>{detail.summary.status}</Badge>}
        >
          <div className="grid gap-3 text-body2 md:grid-cols-2">
            <DefinitionLine label={t("studio.boards.inputObject")} value={detail.definition.inputObject} />
            <DefinitionLine label={t("studio.boards.outputObject")} value={detail.definition.outputObject} />
            <DefinitionLine label={t("studio.boards.signalTypes")} value={detail.definition.signalTypes.join(", ")} />
            <DefinitionLine label={t("studio.boards.visibleSections")} value={detail.definition.visibleSections.join(", ")} />
          </div>
        </StudioPanel>

        <StudioPanel title={t("studio.boards.buildOutput")} description={t("studio.boards.buildDescription")} contentClassName="space-y-3">
          <div className="space-y-1.5">
            <label className="block text-label font-medium text-muted-foreground" htmlFor="board-topic">
              {t("studio.boards.topic")}
            </label>
            <input
              id="board-topic"
              value={topic}
              onChange={(event) => setTopic(event.target.value)}
              className={`${studioFieldClass} h-8 text-[13px]`}
            />
          </div>
          <div className="space-y-1.5">
            <label className="block text-label font-medium text-muted-foreground" htmlFor="board-sample-items">
              {t("studio.boards.sampleItemsJson")}
            </label>
            <textarea
              id="board-sample-items"
              value={itemsJson}
              onChange={(event) => setItemsJson(event.target.value)}
              className={`${studioFieldClass} rk-scroll min-h-[220px] resize-y py-2 font-mono text-label`}
            />
          </div>
          {formError ? (
            <p role="alert" className="text-body2 text-destructive-text">
              {formError}
            </p>
          ) : null}
          <Button onClick={handleBuildOutput} disabled={isLoading}>
            <Play size={15} strokeWidth={1.8} aria-hidden="true" />
            {isLoading ? t("studio.boards.building") : t("studio.boards.buildOutput")}
          </Button>
        </StudioPanel>
      </section>

      {output.crossBoard ? <CrossBoardInsightPanel crossBoard={output.crossBoard} /> : null}
      <BoardOutputPanel output={output} />
    </main>
  )
}

function NoticeList({ notices }: { notices: string[] }) {
  const { t } = useI18n()
  const unique = [...new Set(notices.filter(Boolean))]
  if (!unique.length) return null
  return (
    <div className="space-y-2">
      {unique.map((notice) => (
        <StudioFallbackNotice key={notice} title={t("studio.boards.dataNotice")} message={notice} />
      ))}
    </div>
  )
}

function DefinitionLine({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-label text-muted-foreground">{label}</p>
      <p className="mt-0.5 break-words text-foreground">{value}</p>
    </div>
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}
