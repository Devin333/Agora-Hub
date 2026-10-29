"use client"

import { useEffect, useState } from "react"
import Link from "next/link"
import { ExternalLink, X } from "lucide-react"
import { ErrorState } from "@/components/common/error-state"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { Button } from "@/components/ui/button"
import { ProjectBadges, ProjectDetailContent } from "@/features/projects/components/project-detail-panel"
import { ProjectEmptyText } from "@/features/projects/components/project-page-layout"
import { fetchProjectDetail } from "@/lib/projects/api"
import { cn } from "@/lib/utils"
import type { ProjectItem } from "@/types/projects"

const CLOSE_ANIMATION_MS = 320

export function ProjectDetailDrawer({
  projectSlug,
  open,
  closeHref,
  onOpenChange,
}: {
  projectSlug?: string | null
  open: boolean
  closeHref: string
  onOpenChange: (open: boolean) => void
}) {
  const [project, setProject] = useState<ProjectItem | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || !projectSlug) {
      const timeout = window.setTimeout(() => setProject(null), CLOSE_ANIMATION_MS)
      return () => window.clearTimeout(timeout)
    }

    let cancelled = false
    setIsLoading(true)
    setError(null)
    fetchProjectDetail(projectSlug)
      .then((result) => {
        if (!cancelled) {
          setProject(result)
        }
      })
      .catch((requestError) => {
        if (!cancelled) {
          setError(requestError instanceof Error ? requestError.message : "Project detail request failed.")
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [open, projectSlug])

  useEffect(() => {
    if (!open) return
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onOpenChange(false)
      }
    }
    window.addEventListener("keydown", handleKeyDown)
    return () => window.removeEventListener("keydown", handleKeyDown)
  }, [onOpenChange, open])

  const isVisible = open && Boolean(projectSlug)
  const content = project ? (
    <>
      <header className="border-b border-border px-4 py-4 md:px-6">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="truncate text-label text-muted-foreground">Project Radar / {project.fullName}</p>
            <h2 className="mt-1 break-words text-heading2 font-semibold text-foreground">{project.name}</h2>
            <p className="mt-1.5 max-w-4xl break-words text-body2 text-muted-foreground">{project.description}</p>
            <div className="mt-3">
              <ProjectBadges project={project} showLicense={false} />
            </div>
          </div>
          <Link
            href={closeHref}
            role="button"
            aria-label="Close project detail"
            className="grid size-8 shrink-0 place-items-center rounded-full text-muted-foreground outline-none transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
            onClick={(event) => {
              event.preventDefault()
              onOpenChange(false)
            }}
          >
            <X className="size-4" strokeWidth={1.8} />
          </Link>
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          <Button asChild size="sm">
            <a href={project.repoUrl} target="_blank" rel="noreferrer">
              <ExternalLink strokeWidth={1.8} />
              Open repo
            </a>
          </Button>
          <Button asChild variant="outline" size="sm">
            <Link href={`/projects/${project.slug}`}>Open full detail</Link>
          </Button>
        </div>
      </header>
      <div className="px-4 py-5 md:px-6">
        <ProjectDetailContent project={project} />
      </div>
    </>
  ) : null

  return (
    <>
      <div
        className={cn(
          "fixed inset-0 z-50 bg-overlay backdrop-blur-[4px] transition-[opacity,backdrop-filter] duration-300",
          isVisible ? "opacity-100" : "pointer-events-none opacity-0 backdrop-blur-0"
        )}
        aria-hidden="true"
        onClick={() => onOpenChange(false)}
      />
      <aside
        className={cn(
          "fixed inset-y-0 right-0 z-50 flex w-[min(58rem,96vw)] flex-col border-l border-border bg-background text-foreground shadow-lg transition-[transform,opacity] duration-300 xl:w-[min(76rem,72vw)]",
          isVisible ? "translate-x-0 opacity-100" : "pointer-events-none translate-x-full opacity-0"
        )}
        aria-label="Project detail"
        aria-modal="true"
        role="dialog"
      >
        <div className="rk-scroll min-h-0 flex-1 overflow-y-auto">
          {isLoading ? <div className="px-4 py-5 md:px-6"><PageSkeleton /></div> : null}
          {error ? (
            <div className="px-4 py-5 md:px-6">
              <ErrorState title="Project detail failed to load" message={error} />
            </div>
          ) : null}
          {!isLoading && !error && !project ? (
            <div className="px-4 py-5 md:px-6">
              <ProjectEmptyText title="Project detail unavailable" description="No project detail was returned by the current real data source." />
            </div>
          ) : null}
          {!isLoading && !error ? content : null}
        </div>
      </aside>
    </>
  )
}
