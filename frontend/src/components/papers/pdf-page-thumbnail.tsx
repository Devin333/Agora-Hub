"use client"

import { FileText } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import type { Locale } from "@/lib/papers/types"

type PdfPageThumbnailProps = {
  className?: string
  locale: Locale
  pdfUrl: string
  title: string
}

const MAX_ACTIVE_PDF_RENDERS = 2
let activePdfRenders = 0
const queuedPdfRenders: Array<() => void> = []

export function PdfPageThumbnail({ className, locale, pdfUrl, title }: PdfPageThumbnailProps) {
  const rootRef = useRef<HTMLDivElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const [failed, setFailed] = useState(false)
  const [ready, setReady] = useState(false)
  const [shouldRender, setShouldRender] = useState(false)

  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") {
      setShouldRender(true)
      return
    }

    const root = rootRef.current
    if (!root) {
      return
    }

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setShouldRender(true)
          observer.disconnect()
        }
      },
      { rootMargin: "900px 0px" }
    )
    observer.observe(root)

    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!shouldRender) {
      return
    }

    let cancelled = false
    let cleanup: (() => Promise<void>) | undefined

    async function renderPdfPage() {
      setFailed(false)
      setReady(false)
      const releaseRenderSlot = await acquirePdfRenderSlot()
      if (cancelled) {
        releaseRenderSlot()
        return
      }

      try {
        const pdfjs = await import("pdfjs-dist")
        if (cancelled) return
        pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs"

        const proxyUrl = `/api/papers/pdf?url=${encodeURIComponent(pdfUrl)}`
        const loadingTask = pdfjs.getDocument({
          disableAutoFetch: true,
          disableStream: true,
          rangeChunkSize: 65536,
          url: proxyUrl,
          withCredentials: false
        })
        let destruction: Promise<void> | undefined
        let renderTask: import("pdfjs-dist").RenderTask | undefined = undefined
        // Rendering, unmounting and completion share one resource release.
        cleanup = () => {
          if (!destruction) {
            renderTask?.cancel()
            destruction = loadingTask.destroy()
          }
          return destruction
        }

        const pdf = await loadingTask.promise
        const page = await pdf.getPage(1)
        const viewport = page.getViewport({ scale: 1 })
        const targetWidth = 360
        const scale = targetWidth / viewport.width
        const scaledViewport = page.getViewport({ scale })
        const canvas = canvasRef.current
        const context = canvas?.getContext("2d")

        if (!canvas || !context || cancelled) {
          return
        }

        canvas.width = Math.floor(scaledViewport.width)
        canvas.height = Math.floor(scaledViewport.height)
        canvas.style.width = ""
        canvas.style.height = ""

        renderTask = page.render({
          canvasContext: context,
          viewport: scaledViewport
        })
        await renderTask.promise

        if (!cancelled) {
          setReady(true)
        }
      } catch {
        if (!cancelled) {
          setFailed(true)
        }
      } finally {
        await cleanup?.().catch(() => {
          if (!cancelled) {
            setReady(false)
            setFailed(true)
          }
        })
        releaseRenderSlot()
      }
    }

    void renderPdfPage()

    return () => {
      cancelled = true
      void cleanup?.().catch(() => undefined)
    }
  }, [pdfUrl, shouldRender])

  if (failed) {
    return (
      <div ref={rootRef} className={className}>
        <PdfFallback title={title} locale={locale} />
      </div>
    )
  }

  return (
    <div ref={rootRef} className={className}>
      {!ready ? (
        <PdfLoading locale={locale} />
      ) : null}
      <div className={ready ? "flex h-full w-full items-center justify-center bg-white" : "hidden"}>
        <canvas
          ref={canvasRef}
          aria-label={`${title} PDF first page`}
          className="block max-h-full max-w-full bg-white"
        />
      </div>
    </div>
  )
}

function acquirePdfRenderSlot() {
  if (activePdfRenders < MAX_ACTIVE_PDF_RENDERS) {
    activePdfRenders += 1
    return Promise.resolve(releasePdfRenderSlot)
  }

  return new Promise<() => void>((resolve) => {
    queuedPdfRenders.push(() => {
      activePdfRenders += 1
      resolve(releasePdfRenderSlot)
    })
  })
}

function releasePdfRenderSlot() {
  activePdfRenders = Math.max(0, activePdfRenders - 1)
  const next = queuedPdfRenders.shift()
  if (next) {
    next()
  }
}

function PdfLoading({ locale }: { locale: Locale }) {
  return (
    <div className="flex h-full w-full flex-col justify-between bg-white p-4">
      <div className="space-y-2">
        <span className="block h-2 w-20 rounded-full bg-slate-200" />
        <span className="block h-1.5 w-24 rounded-full bg-slate-100" />
      </div>
      <div className="space-y-2">
        <span className="block h-1.5 w-full rounded-full bg-slate-100" />
        <span className="block h-1.5 w-11/12 rounded-full bg-slate-100" />
        <span className="block h-1.5 w-9/12 rounded-full bg-slate-100" />
      </div>
      <p className="text-[0.58rem] font-semibold uppercase tracking-[0.16em] text-slate-300">
        {locale === "zh" ? "渲染 PDF" : "Rendering PDF"}
      </p>
    </div>
  )
}

function PdfFallback({ locale, title }: { locale: Locale; title: string }) {
  return (
    <div className="flex h-full w-full flex-col bg-white p-4 text-slate-500">
      <div className="flex items-start justify-between gap-3">
        <FileText className="size-6 text-slate-400" />
        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[0.55rem] font-bold uppercase tracking-[0.14em] text-slate-400">
          PDF
        </span>
      </div>
      <div className="mt-6 space-y-2">
        <span className="block h-1.5 w-24 rounded-full bg-slate-200" />
        <span className="block h-1.5 w-20 rounded-full bg-slate-100" />
        <span className="block h-1.5 w-28 rounded-full bg-slate-100" />
      </div>
      <p className="mt-auto line-clamp-3 text-[0.68rem] font-semibold leading-4 text-slate-500">{title}</p>
      <p className="mt-3 text-[0.56rem] font-semibold uppercase tracking-[0.14em] text-slate-400">
        {locale === "zh" ? "PDF 首页暂不可预览" : "PDF first page unavailable"}
      </p>
    </div>
  )
}
