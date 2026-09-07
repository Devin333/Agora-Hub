"use client"

"use client"

import { Fragment, type MouseEvent, type ReactNode } from "react"
import Link from "next/link"
import { Bell, BookMarked, BookOpen, Clock3, ExternalLink, Eye, FileText, Github, GitCompareArrows, Heart, Quote } from "lucide-react"
import { PaperTags } from "@/components/papers/paper-tags"
import { PaperThumbnail } from "@/components/papers/paper-thumbnail"
import { Button } from "@/components/ui/button"
import { translate } from "@/lib/i18n"
import { papersCopy, t } from "@/lib/papers/copy"
import { formatCompactNumber, formatPaperDate, paperPdfUrl, paperSnippet, paperTitle } from "@/lib/papers/format"
import { papersRoutes } from "@/lib/papers/routes"
import { cn } from "@/lib/utils"
import type { Locale, Paper } from "@/lib/papers/types"
import { usePaperWorkspaceStore, type PaperWorkspaceList } from "@/stores/paper-workspace-store"

export function PaperRow({
  paper,
  locale,
  onPreview,
  renderPdfPreview = true,
  className
}: {
  paper: Paper
  locale: Locale
  onPreview: (paper: Paper) => void
  renderPdfPreview?: boolean
  className?: string
}) {
  const pdfHref = paperPdfUrl(paper)
  const repoHref = paper.repoUrl?.startsWith("https://github.com/") && paper.repoUrl !== "https://github.com/" ? paper.repoUrl : undefined
  const tasks = paper.taskRefs ?? []
  const methods = paper.methodRefs ?? []
  const tags = [...new Set(paper.tags ?? [])]
  const metadata = [paper.authors?.slice(0, 3).join(", "), formatPaperDate(paper.publishedAt, locale), paper.venue].filter(Boolean)
  const readingListActive = usePaperWorkspaceStore((state) => state.readingList.includes(paper.id))
  const compareActive = usePaperWorkspaceStore((state) => state.compare.includes(paper.id))
  const laterActive = usePaperWorkspaceStore((state) => state.later.includes(paper.id))
  const togglePaper = usePaperWorkspaceStore((state) => state.togglePaper)

  function handleRowClick(event: MouseEvent<HTMLElement>) {
    const target = event.target
    if (target instanceof Element && target.closest("a, button")) {
      return
    }
    onPreview(paper)
  }

  return (
    <article
      data-testid="paper-row"
      className={cn("group cursor-pointer rounded-xl px-3 py-5 transition-colors first:pt-5 last:pb-3 hover:bg-white/70 sm:px-4 dark:hover:bg-card/45", className)}
      onClick={handleRowClick}
    >
      <div className="grid gap-5 lg:grid-cols-[8.5rem_minmax(0,1fr)] xl:gap-6">
        <div
          role="button"
          tabIndex={0}
          className="group w-fit shrink-0 justify-self-center sm:justify-self-start"
          onClick={() => onPreview(paper)}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault()
              onPreview(paper)
            }
          }}
          aria-label={`Preview ${paper.title}`}
        >
          <PaperThumbnail paper={paper} locale={locale} renderPdfPreview={renderPdfPreview} />
        </div>

        <div className="min-w-0 space-y-4">
          <Link href={papersRoutes.detail(paper.slug || paper.id)} className="block text-left hover:text-primary">
            <h2 className="max-w-4xl text-balance text-xl font-semibold leading-7 text-[#1f2933] sm:text-[1.35rem] sm:leading-8 dark:text-foreground">
              {paperTitle(paper, locale)}
            </h2>
          </Link>

          <p data-paper-field="metadata" className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-sm text-[#334155]/55 dark:text-muted-foreground">
            {metadata.map((item, index) => (
              <Fragment key={`${paper.id}-meta-${index}`}>
                {index > 0 ? <PaperMetaSeparator /> : null}
                <span className={index === 0 ? "font-semibold text-[#334155]/68 dark:text-foreground/80" : undefined}>{item}</span>
              </Fragment>
            ))}
          </p>

          <p data-paper-field="abstract" className="line-clamp-3 max-w-4xl text-[0.95rem] leading-6 text-[#334155]/72 dark:text-muted-foreground">
            {paperSnippet(paper, locale)}
          </p>

          <div>
            <PaperTags tasks={tasks} methods={methods} tags={tags} locale={locale} />
          </div>

          <div data-paper-field="actions" className="flex flex-wrap items-center gap-2.5">
            <PaperActionButton
              type="button"
              ariaLabel={`${translate(locale, "papers.previewPaper")} ${paper.title}`}
              icon={<Eye className="size-4" />}
              label={translate(locale, "papers.previewPaper")}
              onClick={() => onPreview(paper)}
            />
            <PaperActionLink
              href={papersRoutes.reader(paper.slug || paper.id)}
              ariaLabel={`${translate(locale, "papers.readPaper")} ${paper.title}`}
              icon={<BookOpen className="size-4" />}
              label={translate(locale, "papers.readPaper")}
            />
            {pdfHref ? (
              <PaperActionLink
                href={pdfHref}
                external
                ariaLabel={t(papersCopy.openPaper, locale)}
                icon={<FileText className="size-4" />}
                label="PDF"
              />
            ) : null}
            {repoHref ? (
              <PaperActionLink
                href={repoHref}
                external
                ariaLabel={t(papersCopy.openCode, locale)}
                icon={<Github className="size-4" />}
                label={translate(locale, "papers.reader.code")}
                meta={typeof paper.githubStars === "number" ? `${formatCompactNumber(paper.githubStars)} ${translate(locale, "papers.reader.stars")}` : undefined}
              />
            ) : null}
            {typeof paper.citationCount === "number" ? (
              <span className="inline-flex h-8 items-center gap-2 rounded-full border border-transparent bg-[#f4f7f3] px-3 text-xs font-semibold text-[#334155]/65 dark:bg-secondary dark:text-muted-foreground">
                <Quote className="size-3.5" />
                {formatCompactNumber(paper.citationCount)} {translate(locale, "papers.reader.cites")}
              </span>
            ) : null}
          </div>

          <div data-paper-field="workspace" className="flex flex-wrap items-center gap-2">
            <WorkspaceActionButton
              list="readingList"
              active={readingListActive}
              paper={paper}
              locale={locale}
              icon={<BookMarked className="size-3.5" />}
              onToggle={togglePaper}
            />
            <WorkspaceActionButton
              list="compare"
              active={compareActive}
              paper={paper}
              locale={locale}
              icon={<GitCompareArrows className="size-3.5" />}
              onToggle={togglePaper}
            />
            <WorkspaceActionButton
              list="later"
              active={laterActive}
              paper={paper}
              locale={locale}
              icon={<Clock3 className="size-3.5" />}
              onToggle={togglePaper}
            />
          </div>

          {paper.userState?.favorite || paper.userState?.subscribed ? (
            <div className="flex flex-wrap gap-2 text-xs font-semibold text-[#334155]/60 dark:text-muted-foreground">
              {paper.userState.favorite ? (
                <span className="inline-flex items-center gap-1 rounded-sm bg-[#eef4ef] px-2 py-1 dark:bg-secondary">
                  <Heart className="size-3 fill-current" />
                  {translate(locale, "papers.reader.favorite")}
                </span>
              ) : null}
              {paper.userState.subscribed ? (
                <span className="inline-flex items-center gap-1 rounded-sm bg-[#eef4ef] px-2 py-1 dark:bg-secondary">
                  <Bell className="size-3" />
                  {translate(locale, "papers.reader.subscribed")}
                </span>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    </article>
  )
}

function WorkspaceActionButton({
  list,
  active,
  paper,
  locale,
  icon,
  onToggle
}: {
  list: PaperWorkspaceList
  active: boolean
  paper: Paper
  locale: Locale
  icon: ReactNode
  onToggle: (list: PaperWorkspaceList, paperId: string) => void
}) {
  const label = workspaceLabel(list, active, locale)
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      aria-pressed={active}
      aria-label={`${label} ${paper.title}`}
      className={[
        "h-7 rounded-full border-[#dbe3dc] px-2.5 text-left text-[0.72rem] shadow-none",
        active
          ? "bg-[#172033] text-white hover:bg-[#172033]/90 dark:bg-primary dark:text-primary-foreground"
          : "bg-white text-[#334155]/70 hover:bg-[#eef4ef] dark:border-border dark:bg-card dark:text-muted-foreground dark:hover:bg-secondary"
      ].join(" ")}
      onClick={() => onToggle(list, paper.id)}
    >
      <span className={active ? "text-white" : "text-[#334155]/60 dark:text-muted-foreground"}>{icon}</span>
      <span className="font-semibold">{label}</span>
    </Button>
  )
}

function workspaceLabel(list: PaperWorkspaceList, active: boolean, locale: Locale) {
  const key = `papers.workspace.${active ? "remove" : "add"}.${list}`
  return translate(locale, key)
}

function PaperMetaSeparator() {
  return <span aria-hidden="true" className="size-1 rounded-full bg-[#94a3b8]/60" />
}

function PaperActionLink({
  href,
  ariaLabel,
  icon,
  label,
  meta,
  external = false
}: {
  href: string
  ariaLabel: string
  icon: ReactNode
  label: string
  meta?: string
  external?: boolean
}) {
  const content = (
    <>
      <span className="text-[#334155]/65 dark:text-muted-foreground">{icon}</span>
      <span className="font-semibold text-[#334155] dark:text-foreground">{label}</span>
      {meta ? <span className="text-[#334155]/50 dark:text-muted-foreground">{meta}</span> : null}
      {external ? <ExternalLink className="size-3.5 text-[#334155]/45 dark:text-muted-foreground" aria-hidden="true" /> : null}
    </>
  )

  return (
    <Button
      asChild
      variant="outline"
      size="sm"
      aria-label={ariaLabel}
      className="h-8 rounded-full border-[#dbe3dc] bg-white px-3 text-left text-xs shadow-none hover:bg-[#eef4ef] dark:border-border dark:bg-card dark:hover:bg-secondary"
    >
      {external ? (
        <a href={href} target="_blank" rel="noreferrer" className="inline-flex items-center gap-2">
          {content}
        </a>
      ) : (
        <Link href={href} className="inline-flex items-center gap-2">
          {content}
        </Link>
      )}
    </Button>
  )
}

function PaperActionButton({
  ariaLabel,
  icon,
  label,
  onClick,
  type
}: {
  ariaLabel: string
  icon: ReactNode
  label: string
  onClick: () => void
  type: "button"
}) {
  return (
    <Button
      type={type}
      variant="outline"
      size="sm"
      aria-label={ariaLabel}
      className="h-8 rounded-full border-[#dbe3dc] bg-white px-3 text-left text-xs shadow-none hover:bg-[#eef4ef] dark:border-border dark:bg-card dark:hover:bg-secondary"
      onClick={onClick}
    >
      <span className="text-[#334155]/65 dark:text-muted-foreground">{icon}</span>
      <span className="font-semibold text-[#334155] dark:text-foreground">{label}</span>
    </Button>
  )
}
