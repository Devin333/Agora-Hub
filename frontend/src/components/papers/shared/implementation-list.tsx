"use client"

import { Github } from "lucide-react"
import { translate } from "@/lib/i18n"
import { formatCompactNumber, paperTitle } from "@/lib/papers/format"
import type { Locale, Paper } from "@/lib/papers/types"

export function ImplementationList({ papers, locale, title }: { papers: Paper[]; locale: Locale; title: string }) {
  const implementations = implementationsFromPapers(papers, locale)

  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between gap-3 px-1">
        <h2 className="text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">{title}</h2>
        <span className="text-label tabular-nums text-faint-foreground">
          {translate(locale, "papers.reader.repositories", { count: implementations.length })}
        </span>
      </div>
      {implementations.length ? (
        <ul className="divide-y overflow-hidden rounded-xl border">
          {implementations.map((implementation) => (
            <li key={implementation.id}>
              <a
                href={implementation.repoUrl}
                target="_blank"
                rel="noreferrer"
                className="flex items-center justify-between gap-4 px-4 py-3 text-body2 text-foreground outline-none transition-colors hover:bg-accent/40 focus-visible:bg-accent/40"
              >
                <span className="inline-flex min-w-0 items-center gap-3">
                  <Github className="size-4 shrink-0 text-muted-foreground" strokeWidth={1.8} aria-hidden="true" />
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{implementation.name}</span>
                    <span className="mt-0.5 block truncate text-label text-muted-foreground">{implementation.paperTitle}</span>
                  </span>
                </span>
                <span className="shrink-0 text-label tabular-nums text-muted-foreground">
                  {typeof implementation.githubStars === "number"
                    ? `${formatCompactNumber(implementation.githubStars)} ${translate(locale, "papers.reader.stars")}`
                    : translate(locale, "papers.reader.project")}
                </span>
              </a>
            </li>
          ))}
        </ul>
      ) : (
        <p className="px-1 text-body2 text-muted-foreground">{translate(locale, "papers.reader.noImplementations")}</p>
      )}
    </section>
  )
}

function implementationsFromPapers(papers: Paper[], locale: Locale) {
  const seen = new Set<string>()
  return papers.flatMap((paper) => {
    const items = paper.implementations?.length
      ? paper.implementations
      : paper.repoUrl?.startsWith("https://github.com/")
        ? [{ id: `${paper.id}-repo`, name: paper.repoUrl.replace(/^https:\/\/github\.com\//, ""), repoUrl: paper.repoUrl, githubStars: paper.githubStars }]
        : []

    return items
      .filter((item) => {
        if (seen.has(item.repoUrl)) {
          return false
        }
        seen.add(item.repoUrl)
        return true
      })
      .map((item) => ({
        ...item,
        paperTitle: paperTitle(paper, locale)
      }))
  })
}
