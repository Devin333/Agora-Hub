"use client"

import { useEffect, useRef, useState, type ReactNode } from "react"
import { RefreshCw, X } from "lucide-react"
import { PaperDetailContent, PaperDetailEyebrow } from "@/components/papers/shared/paper-detail-content"
import { translate } from "@/lib/i18n"
import { fetchPaperDetail } from "@/lib/papers/api"
import { papersCopy, t } from "@/lib/papers/copy"
import type { Locale, Paper } from "@/lib/papers/types"
import { cn } from "@/lib/utils"

const CLOSE_ANIMATION_MS = 420

export function PaperDetailDrawer({
  paper,
  paperId,
  locale,
  open,
  closeHref,
  returnTo,
  onOpenChange
}: {
  paper: Paper | null
  paperId?: string | null
  locale: Locale
  open: boolean
  closeHref?: string
  returnTo?: string
  onOpenChange: (open: boolean) => void
}) {
  const [activePaper, setActivePaper] = useState<Paper | null>(paper)
  const [detailError, setDetailError] = useState<string | null>(null)

  useEffect(() => {
    if (paper) {
      if (!isPublicPaper(paper)) {
        setActivePaper(null)
        setDetailError(translate(locale, "papers.reader.detailUnavailable"))
        return
      }
      setActivePaper(paper)
      setDetailError(null)
      return
    }
    if (open && paperId) {
      return
    }

    const timeout = window.setTimeout(() => setActivePaper(null), CLOSE_ANIMATION_MS)
    return () => window.clearTimeout(timeout)
  }, [locale, open, paper, paperId])

  useEffect(() => {
    if (!open || !paperId) {
      return
    }
    if (paper && (paper.id === paperId || paper.slug === paperId)) {
      return
    }

    let cancelled = false
    setActivePaper(null)
    setDetailError(null)
    fetchPaperDetail(paperId)
      .then((detail) => {
        if (!cancelled) {
          if (!isPublicPaper(detail)) {
            setActivePaper(null)
            setDetailError(translate(locale, "papers.reader.detailUnavailable"))
            return
          }
          setActivePaper(detail)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setDetailError(translate(locale, "papers.reader.detailUnavailable"))
        }
      })
    return () => {
      cancelled = true
    }
  }, [locale, open, paper, paperId])

  useEffect(() => {
    if (!open) {
      return
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onOpenChange(false)
      }
    }

    window.addEventListener("keydown", handleKeyDown)
    return () => window.removeEventListener("keydown", handleKeyDown)
  }, [onOpenChange, open])

  if (!activePaper && (!open || !paperId)) {
    return null
  }

  const isVisible = open && Boolean(activePaper || paperId)

  if (!activePaper) {
    return (
      <PaperDetailFrame
        isVisible={isVisible}
        locale={locale}
        closeHref={closeHref}
        eyebrow={translate(locale, "papers.reader.paper")}
        onOpenChange={onOpenChange}
      >
        <div className="rk-scroll min-h-0 flex-1 overflow-y-auto px-6 py-6">
          {detailError ? (
            <div role="alert" className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-body2 text-destructive-text">
              {detailError}
            </div>
          ) : (
            <div role="status" className="flex items-center gap-3 rounded-xl border px-4 py-3 text-body2 text-muted-foreground">
              <RefreshCw className="size-4 animate-spin" strokeWidth={1.8} aria-hidden="true" />
              {translate(locale, "papers.reader.loading")}
            </div>
          )}
        </div>
      </PaperDetailFrame>
    )
  }

  return (
    <PaperDetailFrame
      isVisible={isVisible}
      locale={locale}
      closeHref={closeHref}
      eyebrow={<PaperDetailEyebrow paper={activePaper} locale={locale} />}
      onOpenChange={onOpenChange}
    >
      <div className="rk-scroll min-h-0 flex-1 overflow-y-auto px-6 py-6">
        <PaperDetailContent paper={activePaper} locale={locale} detailError={detailError} titleLevel={2} returnTo={returnTo} />
      </div>
    </PaperDetailFrame>
  )
}

function isPublicPaper(paper: Paper) {
  return paper.isPublished !== false
}

function PaperDetailFrame({
  children,
  closeHref,
  eyebrow,
  isVisible,
  locale,
  onOpenChange
}: {
  children: ReactNode
  closeHref?: string
  eyebrow: ReactNode
  isVisible: boolean
  locale: Locale
  onOpenChange: (open: boolean) => void
}) {
  const frame = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!isVisible || !frame.current) return
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const overflow = document.body.style.overflow
    document.body.style.overflow = "hidden"
    const panel = frame.current
    panel.querySelector<HTMLElement>("[role=button]")?.focus({ preventScroll: true })
    function keepFocus(event: KeyboardEvent) {
      if (event.key !== "Tab") return
      const targets = [...panel.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex="0"]')].filter((item) => item.getClientRects().length > 0)
      const first = targets[0]
      const last = targets.at(-1)
      if (event.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) {
        event.preventDefault(); last?.focus()
      } else if (!event.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) {
        event.preventDefault(); first?.focus()
      }
    }
    document.addEventListener("keydown", keepFocus)
    return () => {
      document.body.style.overflow = overflow
      document.removeEventListener("keydown", keepFocus)
      previous?.focus({ preventScroll: true })
    }
  }, [isVisible])
  return (
    <>
      <div
        className={cn(
          "fixed inset-0 z-50 bg-overlay backdrop-blur-[4px] transition-[opacity,backdrop-filter] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)]",
          isVisible ? "opacity-100" : "pointer-events-none opacity-0 backdrop-blur-0"
        )}
        aria-hidden="true"
        onClick={() => onOpenChange(false)}
      />
      <aside
        ref={frame}
        className={cn(
          "fixed inset-y-0 right-0 z-50 flex w-[min(44rem,92vw)] flex-col border-l bg-background text-foreground shadow-lg transition-[transform,opacity] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] lg:w-[min(48rem,64vw)] xl:w-[min(52rem,56vw)] 2xl:w-[56rem]",
          isVisible ? "translate-x-0 opacity-100" : "pointer-events-none translate-x-full opacity-0"
        )}
        aria-label="Paper detail"
        aria-modal="true"
        aria-hidden={!isVisible}
        role="dialog"
      >
        <div className="flex items-center justify-between gap-3 border-b px-6 py-3">
          <div className="min-w-0 truncate text-label font-medium text-muted-foreground">{eyebrow}</div>
          <a
            href={closeHref ?? "/papers"}
            role="button"
            className="grid size-8 place-items-center rounded-full text-muted-foreground outline-none transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
            aria-label={t(papersCopy.dismiss, locale)}
            onClick={(event) => {
              event.preventDefault()
              onOpenChange(false)
            }}
          >
            <X className="size-4" strokeWidth={1.8} aria-hidden="true" />
          </a>
        </div>

        {children}
      </aside>
    </>
  )
}
