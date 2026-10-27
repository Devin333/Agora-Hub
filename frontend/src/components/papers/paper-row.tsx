"use client"

import { Fragment, type MouseEvent, type ReactNode } from "react"
import Link from "next/link"
import { Bell, BookMarked, BookOpen, Clock3, ExternalLink, Eye, FileText, Github, GitCompareArrows, Heart, Quote } from "lucide-react"
import { PaperTags } from "@/components/papers/paper-tags"
import { PaperThumbnail } from "@/components/papers/paper-thumbnail"
import { Badge } from "@/components/ui/badge"
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
      className={cn("group cursor-pointer px-4 py-4 transition-colors hover:bg-accent/40", className)}
      onClick={handleRowClick}
    >
      <div className="grid gap-4 lg:grid-cols-[8.5rem_minmax(0,1fr)] xl:gap-5">
        <div
          role="button"
          tabIndex={0}
          className="group w-fit shrink-0 justify-self-center rounded-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50 sm:justify-self-start"
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

        <div className="min-w-0 space-y-3">
          <Link href={papersRoutes.detail(paper.slug || paper.id)} className="block rounded-sm text-left outline-none underline-offset-4 hover:underline focus-visible:ring-3 focus-visible:ring-ring/50">
            <h2 className="max-w-4xl text-balance text-heading3 font-semibold text-foreground">
              {paperTitle(paper, locale)}
            </h2>
          </Link>

          <p data-paper-field="metadata" className="flex flex-wrap items-center gap-x-2 gap-y-1 text-label text-muted-foreground">
            {metadata.map((item, index) => (
              <Fragment key={`${paper.id}-meta-${index}`}>
                {index > 0 ? <PaperMetaSeparator /> : null}
                <span className={index === 0 ? "font-medium text-foreground/80" : undefined}>{item}</span>
              </Fragment>
            ))}
          </p>

          <p data-paper-field="abstract" className="line-clamp-3 max-w-4xl text-body2 text-muted-foreground">
            {paperSnippet(paper, locale)}
          </p>

          <div>
            <PaperTags tasks={tasks} methods={methods} tags={tags} locale={locale} />
          </div>

          <div data-paper-field="actions" className="flex flex-wrap items-center gap-1.5">
            <PaperActionButton
              type="button"
              ariaLabel={`${translate(locale, "papers.previewPaper")} ${paper.title}`}
              icon={<Eye className="size-3.5" strokeWidth={1.8} />}
              label={translate(locale, "papers.previewPaper")}
              onClick={() => onPreview(paper)}
            />
            <PaperActionLink
              href={papersRoutes.reader(paper.slug || paper.id)}
              ariaLabel={`${translate(locale, "papers.readPaper")} ${paper.title}`}
              icon={<BookOpen className="size-3.5" strokeWidth={1.8} />}
              label={translate(locale, "papers.readPaper")}
            />
            {pdfHref ? (
              <PaperActionLink
                href={pdfHref}
                external
                ariaLabel={t(papersCopy.openPaper, locale)}
                icon={<FileText className="size-3.5" strokeWidth={1.8} />}
                label="PDF"
              />
            ) : null}
            {repoHref ? (
              <PaperActionLink
                href={repoHref}
                external
                ariaLabel={t(papersCopy.openCode, locale)}
                icon={<Github className="size-3.5" strokeWidth={1.8} />}
                label={translate(locale, "papers.reader.code")}
                meta={typeof paper.githubStars === "number" ? `${formatCompactNumber(paper.githubStars)} ${translate(locale, "papers.reader.stars")}` : undefined}
              />
            ) : null}
            {typeof paper.citationCount === "number" ? (
              <span className="inline-flex h-7 items-center gap-1.5 rounded-full bg-muted px-2.5 text-label font-medium tabular-nums text-muted-foreground">
                <Quote className="size-3.5" strokeWidth={1.8} aria-hidden="true" />
                {formatCompactNumber(paper.citationCount)} {translate(locale, "papers.reader.cites")}
              </span>
            ) : null}
          </div>

          <div data-paper-field="workspace" className="flex flex-wrap items-center gap-1.5">
            <WorkspaceActionButton
              list="readingList"
              active={readingListActive}
              paper={paper}
              locale={locale}
              icon={<BookMarked className="size-3.5" strokeWidth={1.8} />}
              onToggle={togglePaper}
            />
            <WorkspaceActionButton
              list="compare"
              active={compareActive}
              paper={paper}
              locale={locale}
              icon={<GitCompareArrows className="size-3.5" strokeWidth={1.8} />}
              onToggle={togglePaper}
            />
            <WorkspaceActionButton
              list="later"
              active={laterActive}
              paper={paper}
              locale={locale}
              icon={<Clock3 className="size-3.5" strokeWidth={1.8} />}
              onToggle={togglePaper}
            />
          </div>

          {paper.userState?.favorite || paper.userState?.subscribed ? (
            <div className="flex flex-wrap gap-1.5">
              {paper.userState.favorite ? (
                <Badge variant="muted">
                  <Heart className="fill-current" aria-hidden="true" />
                  {translate(locale, "papers.reader.favorite")}
                </Badge>
              ) : null}
              {paper.userState.subscribed ? (
                <Badge variant="muted">
                  <Bell aria-hidden="true" />
                  {translate(locale, "papers.reader.subscribed")}
                </Badge>
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
      variant={active ? "default" : "outline"}
      size="xs"
      aria-pressed={active}
      aria-label={`${label} ${paper.title}`}
      className={cn("rounded-full px-2.5 text-left", !active && "text-muted-foreground hover:text-foreground")}
      onClick={() => onToggle(list, paper.id)}
    >
      {icon}
      <span>{label}</span>
    </Button>
  )
}

function workspaceLabel(list: PaperWorkspaceList, active: boolean, locale: Locale) {
  const key = `papers.workspace.${active ? "remove" : "add"}.${list}`
  return translate(locale, key)
}

function PaperMetaSeparator() {
  return <span aria-hidden="true" className="size-1 rounded-full bg-faint-foreground/60" />
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
      <span className="text-muted-foreground">{icon}</span>
      <span>{label}</span>
      {meta ? <span className="tabular-nums text-muted-foreground">{meta}</span> : null}
      {external ? <ExternalLink className="size-3 text-faint-foreground" strokeWidth={1.8} aria-hidden="true" /> : null}
    </>
  )

  return (
    <Button
      asChild
      variant="outline"
      size="sm"
      aria-label={ariaLabel}
      className="rounded-full px-2.5 text-left"
    >
      {external ? (
        <a href={href} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5">
          {content}
        </a>
      ) : (
        <Link href={href} className="inline-flex items-center gap-1.5">
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
      className="rounded-full px-2.5 text-left"
      onClick={onClick}
    >
      <span className="text-muted-foreground">{icon}</span>
      <span>{label}</span>
    </Button>
  )
}
