"use client"

import { useEffect, useMemo, useState } from "react"
import Link from "next/link"
import { AlertTriangle, ArrowLeft, RefreshCw } from "lucide-react"
import { OpenReaderPage } from "@/components/papers/open-reader"
import { translate } from "@/lib/i18n"
import { formatPaperDate, paperTitle } from "@/lib/papers/format"
import type { Locale } from "@/lib/papers/types"
import { paperDocumentToOpenReader } from "@/lib/paper-reader/open-reader-adapter"
import { fetchPaperDocument, triggerPaperCompile } from "@/lib/paper-reader/api"
import type { PaperCompileTriggerResponse, PaperDiagnostic, PaperDocumentResponse } from "@/lib/paper-reader/types"
import styles from "./paper-document-reader.module.css"

const DOCUMENT_POLL_INTERVAL_MS = 5000

export function PaperDocumentReaderPage({ payload, locale, backHref = "/papers" }: { payload: PaperDocumentResponse; locale: Locale; backHref?: string }) {
  const [currentPayload, setCurrentPayload] = useState(payload)

  useEffect(() => {
    setCurrentPayload(payload)
  }, [payload])

  const compiled = hasCompiledDocument(currentPayload)

  useEffect(() => {
    if (compiled) {
      return undefined
    }

    let cancelled = false
    let inFlight: AbortController | null = null

    async function refreshDocument() {
      inFlight?.abort()
      const controller = new AbortController()
      inFlight = controller

      try {
        const nextPayload = await fetchPaperDocument(currentPayload.paper.id, {
          signal: controller.signal,
        })
        if (!cancelled) {
          setCurrentPayload(nextPayload)
        }
      } catch {
        // The status gate already explains that the document is still being prepared.
      }
    }

    void refreshDocument()
    const interval = window.setInterval(() => {
      void refreshDocument()
    }, DOCUMENT_POLL_INTERVAL_MS)

    return () => {
      cancelled = true
      window.clearInterval(interval)
      inFlight?.abort()
    }
  }, [compiled, currentPayload.paper.id])

  const adapted = useMemo(() => (compiled ? paperDocumentToOpenReader(currentPayload) : null), [compiled, currentPayload])

  if (adapted) {
    return <OpenReaderPage reader={adapted.reader} locale={locale} visualLayer={adapted.visualLayer} backHref={backHref} />
  }

  return <CompileStatusPage payload={currentPayload} locale={locale} backHref={backHref} />
}

function hasCompiledDocument(payload: PaperDocumentResponse) {
  return payload.status.status === "compiled" && Boolean(payload.document && payload.manifest)
}

function CompileStatusPage({ payload, locale, backHref }: { payload: PaperDocumentResponse; locale: Locale; backHref: string }) {
  const { paper, status } = payload
  const [queued, setQueued] = useState<PaperCompileTriggerResponse["enqueued"] | null>(null)
  const [compileError, setCompileError] = useState<string | null>(null)
  const [isPending, setPending] = useState(false)
  const title = paperTitle(paper, locale)

  async function requestCompile(force: boolean) {
    setPending(true)
    setCompileError(null)
    try {
      const result = await triggerPaperCompile(paper.id, { force })
      setQueued(result.enqueued)
    } catch (error) {
      setQueued(null)
      setCompileError(error instanceof Error ? error.name : "compile_request_failed")
    } finally {
      setPending(false)
    }
  }

  return (
    <main className={styles.readerShell}>
      <header className={styles.header}>
        <Link className={styles.backLink} href={backHref} aria-label={translate(locale, "papers.reader.backToPapers")}>
          <ArrowLeft size={18} aria-hidden="true" />
        </Link>
        <div className={styles.headerText}>
          <div className={styles.kicker}>{translate(locale, "papers.reader.readerTitle")}</div>
          <h1>{title}</h1>
          <p>{paper.authors?.join(", ") || translate(locale, "papers.reader.unknownAuthors")} / {paper.venue ?? translate(locale, "papers.reader.paper")} / {formatPaperDate(paper.publishedAt, locale)}</p>
        </div>
        <StatusBadge status={queued ? "queued" : status.status} locale={locale} />
      </header>

      <StatusGate
        status={queued ? "queued" : status.status}
        diagnostics={status.diagnostics}
        reviewSummary={status.reviewReport?.summary}
        queued={queued}
        error={compileError}
        locale={locale}
        onCompile={() => void requestCompile(false)}
        onRecompile={() => void requestCompile(true)}
        pending={isPending}
      />
    </main>
  )
}

function StatusGate({
  status,
  diagnostics,
  reviewSummary,
  queued,
  error,
  locale,
  onCompile,
  onRecompile,
  pending,
}: {
  status: string
  diagnostics: PaperDiagnostic[]
  reviewSummary?: string
  queued: PaperCompileTriggerResponse["enqueued"] | null
  error: string | null
  locale: Locale
  onCompile: () => void
  onRecompile: () => void
  pending: boolean
}) {
  const compileInProgress = queued || status === "queued" || status === "compiling"
  const actionsDisabled = pending || Boolean(compileInProgress)
  const visibleDiagnostics = [...new Set(diagnostics.slice(0, 8).map((item) => readerDiagnosticMessage(item, locale)))]
  return (
    <section className={styles.statusGate}>
      <div className={styles.statusIcon}><AlertTriangle size={22} aria-hidden="true" /></div>
      <div>
        <h2>{translate(locale, "papers.reader.compileUnavailable")}</h2>
        <p>{translate(locale, "papers.reader.statusLabel")}: <strong>{formatReaderStatus(status, locale)}</strong></p>
        {queued ? (
          <p>{translate(locale, "papers.reader.compileTaskQueued")}: <strong>{queued.task_id}</strong></p>
        ) : null}
        {error ? (
          <p role="alert">{translate(locale, "papers.reader.compileActionUnavailable")}</p>
        ) : null}
        {reviewSummary ? <p>{reviewSummary}</p> : null}
        {visibleDiagnostics.length ? (
          <ul>{visibleDiagnostics.map((message) => <li key={message}>{message}</li>)}</ul>
        ) : null}
        <div className={styles.statusActions}>
          <button type="button" onClick={onCompile} disabled={actionsDisabled}>
            <RefreshCw size={16} aria-hidden="true" />{pending ? translate(locale, "papers.reader.queueing") : translate(locale, "papers.reader.compile")}
          </button>
          <button type="button" onClick={onRecompile} disabled={actionsDisabled}>
            <RefreshCw size={16} aria-hidden="true" />{translate(locale, "papers.reader.recompile")}
          </button>
        </div>
      </div>
    </section>
  )
}

function StatusBadge({ status, locale }: { status: string; locale: Locale }) {
  return (
    <span className={`${styles.statusBadge} ${status === "compiled" ? styles.statusCompiled : styles.statusPending}`}>
      {formatReaderStatus(status, locale)}
    </span>
  )
}

function formatReaderStatus(status: string, locale: Locale) {
  switch (status) {
    case "compiled":
      return translate(locale, "papers.reader.statusCompiled")
    case "not_compiled":
      return translate(locale, "papers.reader.statusNotCompiled")
    case "queued":
      return translate(locale, "papers.reader.statusQueued")
    case "compiling":
      return translate(locale, "papers.reader.statusCompiling")
    case "needs_review":
      return translate(locale, "papers.reader.statusNeedsReview")
    case "compile_failed":
    case "review_failed":
    case "failed":
      return translate(locale, "papers.reader.statusFailed")
    default:
      return translate(locale, "papers.reader.statusPending")
  }
}

function readerDiagnosticMessage(diagnostic: PaperDiagnostic, locale: Locale) {
  const code = diagnostic.code.toLowerCase()
  if (code.includes("asset")) {
    return translate(locale, "papers.reader.diagnosticAssetUnavailable")
  }
  if (code.startsWith("source_pdf_")) {
    return translate(locale, "papers.reader.noVerifiedPdf")
  }

  return translate(locale, "papers.reader.diagnosticUnavailable")
}
