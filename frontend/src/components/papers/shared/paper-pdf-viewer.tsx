"use client"

import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react"
import { ChevronLeft, ChevronRight, FileWarning, Loader2, Search, ZoomIn, ZoomOut } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import type { Locale, PaperReaderNote, PaperReaderNoteCreate, PaperReaderNoteRect } from "@/lib/papers/types"

type PaperPdfViewerProps = {
  pdfUrl: string
  title: string
  locale: Locale
  fallback: ReactNode
  paperId?: string
  initialPage?: number
  notes?: PaperReaderNote[]
  onCreateReaderNote?: (note: PaperReaderNoteCreate) => void
  onPageChange?: (pageNumber: number, numPages: number) => void
  className?: string
  errorMessage?: string
}

type PdfViewport = {
  width: number
  height: number
}

type PdfRenderTask = {
  promise: Promise<void>
  cancel: () => void
}

type PdfTextLayer = {
  cancel: () => void
  render: () => Promise<void>
}

type PdfPage = {
  getTextContent?: () => Promise<PdfTextContent>
  getViewport: (options: { scale: number }) => PdfViewport
  render: (params: { canvasContext: CanvasRenderingContext2D; viewport: PdfViewport }) => PdfRenderTask
}

type PdfDocument = {
  numPages: number
  getPage: (pageNumber: number) => Promise<PdfPage>
  destroy: () => Promise<void> | void
}

type PdfLoadingTask = {
  promise: Promise<PdfDocument>
  destroy: () => Promise<void> | void
}

type PdfTextContent = {
  items: Array<{ str?: string }>
}

type PdfPageText = {
  pageNumber: number
  text: string
}

type PdfSearchResult = {
  id: string
  pageNumber: number
  snippet: string
  matchIndex: number
}

type PdfTextSelection = {
  pageNumber: number
  quote: string
  rects: PaperReaderNoteRect[]
}

const MIN_SCALE = 0.75
const MAX_SCALE = 2
const SCALE_STEP = 0.25
const THUMBNAIL_WIDTH = 88
const THUMBNAIL_INTERSECTION_ROOT_MARGIN = "240px"
const MAX_THUMBNAIL_RENDER_CONCURRENCY = 2
const MIN_SEARCH_LENGTH = 2
const SEARCH_CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000
const SEARCH_CACHE_MAX_BYTES = 2 * 1024 * 1024
const SEARCH_CACHE_MAX_ITEMS = 20
const SEARCH_CACHE_INDEX_KEY = "newsroom:paper-pdf-search:index:v1"

type ThumbnailRenderJob = {
  activeCancel?: () => void
  cancelled: boolean
  run: (context: ThumbnailRenderJobContext) => Promise<void>
  started: boolean
}

type ThumbnailRenderJobContext = {
  isCancelled: () => boolean
  setActiveCancel: (cancel: () => void) => void
}

const thumbnailRenderQueue: ThumbnailRenderJob[] = []
let activeThumbnailRenderJobs = 0

export function PaperPdfViewer({
  pdfUrl,
  title,
  locale,
  fallback,
  paperId,
  initialPage,
  notes = [],
  onCreateReaderNote,
  onPageChange,
  className,
  errorMessage,
}: PaperPdfViewerProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const pageLayerRef = useRef<HTMLDivElement | null>(null)
  const textLayerRef = useRef<HTMLDivElement | null>(null)
  const renderTaskRef = useRef<PdfRenderTask | null>(null)
  const textLayerRenderRef = useRef<PdfTextLayer | null>(null)
  const initialPageRef = useRef(initialPage)
  const initialPageAppliedRef = useRef(false)
  const userNavigatedRef = useRef(false)
  const [pdfDocument, setPdfDocument] = useState<PdfDocument | null>(null)
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading")
  const [isRendering, setIsRendering] = useState(false)
  const [pageNumber, setPageNumber] = useState(1)
  const [pageInput, setPageInput] = useState("1")
  const [numPages, setNumPages] = useState(0)
  const [scale, setScale] = useState(1)
  const [pageTexts, setPageTexts] = useState<PdfPageText[]>([])
  const [searchQuery, setSearchQuery] = useState("")
  const [searchStatus, setSearchStatus] = useState<"idle" | "indexing" | "ready" | "error">("idle")
  const [activeSearchIndex, setActiveSearchIndex] = useState(0)
  const [textLayerStatus, setTextLayerStatus] = useState<"idle" | "rendering" | "ready" | "error">("idle")
  const [selectionDraft, setSelectionDraft] = useState<PdfTextSelection | null>(null)
  const [selectionNoteText, setSelectionNoteText] = useState("")
  const searchResults = useMemo(() => buildSearchResults(pageTexts, searchQuery), [pageTexts, searchQuery])
  const currentPageNotes = useMemo(
    () => notes.filter((note) => note.pageNumber === pageNumber && note.anchor?.rects?.length),
    [notes, pageNumber]
  )

  useEffect(() => {
    initialPageRef.current = initialPage
  }, [initialPage])

  useEffect(() => {
    let cancelled = false
    let loadedDocument: PdfDocument | null = null
    let loadingTask: PdfLoadingTask | null = null

    setStatus("loading")
    setIsRendering(false)
    setPdfDocument(null)
    setNumPages(0)
    setPageNumber(1)
    setPageInput("1")
    setScale(1)
    setPageTexts([])
    setSearchQuery("")
    setSearchStatus("idle")
    setActiveSearchIndex(0)
    setTextLayerStatus("idle")
    setSelectionDraft(null)
    setSelectionNoteText("")
    initialPageAppliedRef.current = false
    userNavigatedRef.current = false
    textLayerRenderRef.current?.cancel()
    textLayerRenderRef.current = null
    renderTaskRef.current?.cancel()
    renderTaskRef.current = null

    async function loadPdf() {
      try {
        const pdfjs = await import("pdfjs-dist")
        pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs"
        loadingTask = pdfjs.getDocument({
          disableAutoFetch: true,
          disableStream: true,
          rangeChunkSize: 65536,
          url: pdfProxyUrl(pdfUrl),
          withCredentials: false,
        }) as unknown as PdfLoadingTask
        const pdf = await loadingTask.promise
        loadedDocument = pdf
        if (cancelled) {
          await pdf.destroy()
          return
        }
        const firstPage = clampPage(initialPageRef.current ?? 1, pdf.numPages)
        setPdfDocument(pdf)
        setNumPages(pdf.numPages)
        setPageNumber(firstPage)
        setPageInput(String(firstPage))
        initialPageAppliedRef.current = initialPageRef.current !== undefined
        setStatus("ready")
      } catch {
        if (!cancelled) {
          setStatus("error")
        }
      }
    }

    void loadPdf()

    return () => {
      cancelled = true
      renderTaskRef.current?.cancel()
      renderTaskRef.current = null
      textLayerRenderRef.current?.cancel()
      textLayerRenderRef.current = null
      void loadingTask?.destroy()
      void loadedDocument?.destroy()
    }
  }, [pdfUrl])

  useEffect(() => {
    if (!pdfDocument || status !== "ready" || numPages <= 0) {
      return
    }

    let cancelled = false
    setSearchStatus("indexing")
    setPageTexts([])

    async function indexPdfText() {
      try {
        const cacheKey = searchCacheKey({ paperId, pdfUrl })
        const cachedPages = readSearchCache(cacheKey, numPages)
        if (cachedPages) {
          setPageTexts(cachedPages)
          setSearchStatus("ready")
          return
        }
        const indexedPages: PdfPageText[] = []
        for (let index = 1; index <= numPages; index += 1) {
          const page = await pdfDocument!.getPage(index)
          if (!page.getTextContent) {
            throw new Error("PDF text content unavailable")
          }
          const content = await page.getTextContent()
          if (cancelled) {
            return
          }
          indexedPages.push({
            pageNumber: index,
            text: content.items.map((item) => item.str ?? "").filter(Boolean).join(" "),
          })
        }
        if (!cancelled) {
          writeSearchCache(cacheKey, indexedPages)
          setPageTexts(indexedPages)
          setSearchStatus("ready")
        }
      } catch {
        if (!cancelled) {
          setPageTexts([])
          setSearchStatus("error")
        }
      }
    }

    void indexPdfText()

    return () => {
      cancelled = true
    }
  }, [numPages, paperId, pdfDocument, pdfUrl, status])

  useEffect(() => {
    if (
      initialPage === undefined ||
      initialPageAppliedRef.current ||
      userNavigatedRef.current ||
      status !== "ready" ||
      !pdfDocument ||
      numPages <= 0
    ) {
      return
    }
    const firstPage = clampPage(initialPage, numPages)
    setPageNumber(firstPage)
    setPageInput(String(firstPage))
    initialPageAppliedRef.current = true
  }, [initialPage, numPages, pdfDocument, status])

  useEffect(() => {
    if (!pdfDocument || status !== "ready") {
      return
    }

    let cancelled = false
    renderTaskRef.current?.cancel()
    renderTaskRef.current = null
    textLayerRenderRef.current?.cancel()
    textLayerRenderRef.current = null
    setTextLayerStatus("rendering")
    setSelectionDraft(null)

    async function renderPage() {
      setIsRendering(true)
      try {
        const page = await pdfDocument!.getPage(pageNumber)
        if (cancelled) {
          return
        }
        const viewport = page.getViewport({ scale })
        const canvas = canvasRef.current
        const context = canvas?.getContext("2d")
        if (!canvas || !context) {
          throw new Error("PDF canvas context unavailable")
        }
        const outputScale = typeof window === "undefined" ? 1 : window.devicePixelRatio || 1
        canvas.width = Math.floor(viewport.width * outputScale)
        canvas.height = Math.floor(viewport.height * outputScale)
        canvas.style.width = `${Math.floor(viewport.width)}px`
        canvas.style.height = `${Math.floor(viewport.height)}px`
        context.setTransform(outputScale, 0, 0, outputScale, 0, 0)
        const renderTask = page.render({ canvasContext: context, viewport })
        renderTaskRef.current = renderTask
        await renderTask.promise
        if (!cancelled) {
          await renderTextLayerForPage({ page, viewport })
        }
      } catch {
        if (!cancelled) {
          setStatus("error")
        }
      } finally {
        if (!cancelled) {
          setIsRendering(false)
        }
      }
    }

    async function renderTextLayerForPage({ page, viewport }: { page: PdfPage; viewport: PdfViewport }) {
      const layer = textLayerRef.current
      if (!layer || !page.getTextContent) {
        setTextLayerStatus("error")
        return
      }
      layer.replaceChildren()
      layer.style.width = `${Math.floor(viewport.width)}px`
      layer.style.height = `${Math.floor(viewport.height)}px`
      try {
        const content = await page.getTextContent()
        if (cancelled) {
          return
        }
        const pdfjs = await import("pdfjs-dist")
        const TextLayer = (pdfjs as unknown as {
          TextLayer?: new (params: { textContentSource: PdfTextContent; container: HTMLElement; viewport: PdfViewport }) => PdfTextLayer
        }).TextLayer
        if (TextLayer) {
          const textLayer = new TextLayer({ textContentSource: content, container: layer, viewport })
          textLayerRenderRef.current = textLayer
          await textLayer.render()
        } else {
          renderSimpleTextLayer(layer, content)
        }
        if (!cancelled) {
          setTextLayerStatus("ready")
        }
      } catch {
        if (!cancelled) {
          layer.replaceChildren()
          setTextLayerStatus("error")
        }
      }
    }

    void renderPage()

    return () => {
      cancelled = true
      renderTaskRef.current?.cancel()
      renderTaskRef.current = null
      textLayerRenderRef.current?.cancel()
      textLayerRenderRef.current = null
    }
  }, [pageNumber, pdfDocument, scale, status])

  useEffect(() => {
    if (status === "ready" && numPages > 0) {
      onPageChange?.(pageNumber, numPages)
    }
  }, [numPages, onPageChange, pageNumber, status])

  useEffect(() => {
    setActiveSearchIndex((current) => clampSearchIndex(current, searchResults.length))
  }, [searchResults.length])

  useEffect(() => {
    applySearchHighlights(textLayerRef.current, pageNumber, searchQuery, searchResults, activeSearchIndex)
  }, [activeSearchIndex, pageNumber, searchQuery, searchResults, textLayerStatus])

  if (status === "error") {
    return (
      <div className="min-h-[42rem]">
        <div className="border-b border-[#d8dfd8] bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-border dark:bg-amber-950/30 dark:text-amber-100">
          <div className="flex items-center gap-2 font-semibold">
            <FileWarning className="size-4" />
            {errorMessage || (locale === "zh" ? "PDF 无法渲染，已切换到文本阅读。" : "PDF could not be rendered; showing text fallback.")}
          </div>
        </div>
        {fallback}
      </div>
    )
  }

  const canGoPrevious = status === "ready" && pageNumber > 1
  const canGoNext = status === "ready" && numPages > 0 && pageNumber < numPages
  const canZoomOut = status === "ready" && scale > MIN_SCALE
  const canZoomIn = status === "ready" && scale < MAX_SCALE

  function goToPage(nextPage: number, options: { userInitiated?: boolean } = {}) {
    if (options.userInitiated !== false) {
      userNavigatedRef.current = true
    }
    const clamped = clampPage(nextPage, numPages)
    setPageNumber(clamped)
    setPageInput(String(clamped))
  }

  function handlePageSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    goToPage(Number.parseInt(pageInput, 10))
  }

  function changeScale(nextScale: number) {
    userNavigatedRef.current = true
    setScale(clampScale(nextScale))
  }

  function goToSearchResult(nextIndex: number) {
    if (!searchResults.length) {
      return
    }
    const clamped = clampSearchIndex(nextIndex, searchResults.length)
    setActiveSearchIndex(clamped)
    goToPage(searchResults[clamped].pageNumber)
  }

  function handleTextSelection() {
    const draft = readCurrentTextSelection(pageLayerRef.current, textLayerRef.current, pageNumber)
    setSelectionDraft(draft)
    setSelectionNoteText("")
  }

  function createSelectionNote(kind: "highlight" | "note") {
    if (!selectionDraft || !onCreateReaderNote) {
      return
    }
    const trimmedNote = selectionNoteText.trim()
    onCreateReaderNote({
      kind,
      pageNumber: selectionDraft.pageNumber,
      color: kind === "highlight" ? "yellow" : "blue",
      quote: selectionDraft.quote,
      noteText: kind === "note" ? trimmedNote || selectionDraft.quote : undefined,
      anchor: {
        pageNumber: selectionDraft.pageNumber,
        quote: selectionDraft.quote,
        rects: selectionDraft.rects,
      },
    })
    setSelectionDraft(null)
    setSelectionNoteText("")
    clearWindowSelection()
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (status !== "ready") {
      return
    }
    const target = event.target
    if (
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement
    ) {
      return
    }
    if (event.key === "ArrowLeft" || event.key === "PageUp") {
      event.preventDefault()
      goToPage(pageNumber - 1)
    } else if (event.key === "ArrowRight" || event.key === "PageDown") {
      event.preventDefault()
      goToPage(pageNumber + 1)
    } else if (event.key === "+" || event.key === "=") {
      event.preventDefault()
      changeScale(scale + SCALE_STEP)
    } else if (event.key === "-" || event.key === "_") {
      event.preventDefault()
      changeScale(scale - SCALE_STEP)
    }
  }

  return (
    <div
      aria-label={locale === "zh" ? `${title} PDF 阅读器` : `${title} PDF viewer`}
      className={`flex min-h-[42rem] flex-col bg-[#f8faf9] outline-none focus-visible:ring-2 focus-visible:ring-[#315d8a]/35 dark:bg-background ${className || ""}`}
      tabIndex={0}
      onKeyDown={handleKeyDown}
    >
      <div data-pdf-toolbar className="flex flex-col gap-3 border-b border-[#d8dfd8] bg-white px-4 py-3 dark:border-border dark:bg-card lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold text-[#334155] dark:text-foreground" title={title}>{title}</h2>
          <p className="mt-1 text-xs text-[#334155]/55 dark:text-muted-foreground">
            {status === "ready"
              ? locale === "zh"
                ? `第 ${pageNumber} / ${numPages} 页`
                : `Page ${pageNumber} of ${numPages}`
              : locale === "zh"
                ? "正在加载 PDF"
                : "Loading PDF"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="icon"
            aria-label={locale === "zh" ? "上一页" : "Previous page"}
            disabled={!canGoPrevious}
            onClick={() => goToPage(pageNumber - 1)}
          >
            <ChevronLeft className="size-4" />
          </Button>
          <form className="flex items-center gap-2" onSubmit={handlePageSubmit}>
            <Input
              aria-label={locale === "zh" ? "页码" : "Page number"}
              className="h-9 w-20 bg-white text-center dark:bg-background"
              disabled={status !== "ready"}
              min={1}
              max={numPages || 1}
              type="number"
              value={pageInput}
              onChange={(event) => setPageInput(event.target.value)}
            />
            <span className="text-xs text-[#334155]/55 dark:text-muted-foreground">/ {numPages || "-"}</span>
          </form>
          <Button
            type="button"
            variant="outline"
            size="icon"
            aria-label={locale === "zh" ? "下一页" : "Next page"}
            disabled={!canGoNext}
            onClick={() => goToPage(pageNumber + 1)}
          >
            <ChevronRight className="size-4" />
          </Button>
          <span className="mx-1 h-6 w-px bg-[#d8dfd8] dark:bg-border" />
          <Button
            type="button"
            variant="outline"
            size="icon"
            aria-label={locale === "zh" ? "缩小" : "Zoom out"}
            disabled={!canZoomOut}
            onClick={() => changeScale(scale - SCALE_STEP)}
          >
            <ZoomOut className="size-4" />
          </Button>
          <span className="w-14 text-center text-xs font-semibold text-[#334155]/70 dark:text-muted-foreground">
            {Math.round(scale * 100)}%
          </span>
          <Button
            type="button"
            variant="outline"
            size="icon"
            aria-label={locale === "zh" ? "放大" : "Zoom in"}
            disabled={!canZoomIn}
            onClick={() => changeScale(scale + SCALE_STEP)}
          >
            <ZoomIn className="size-4" />
          </Button>
        </div>
      </div>
      <PdfSearchPanel
        activeIndex={activeSearchIndex}
        locale={locale}
        onQueryChange={(nextQuery) => {
          setSearchQuery(nextQuery)
          setActiveSearchIndex(0)
        }}
        onSelectResult={goToSearchResult}
        query={searchQuery}
        results={searchResults}
        searchStatus={searchStatus}
      />

      <div data-pdf-pages className="grid min-h-[38rem] flex-1 lg:grid-cols-[7.25rem_minmax(0,1fr)]">
        <PdfThumbnailRail
          currentPage={pageNumber}
          locale={locale}
          numPages={numPages}
          onSelectPage={(nextPage) => goToPage(nextPage)}
          pdfDocument={pdfDocument}
          status={status}
          title={title}
        />
        <div data-pdf-canvas-scroll className="relative flex h-[78vh] min-h-[38rem] flex-1 justify-center overflow-auto bg-[#eef4ef] p-4 dark:bg-secondary/30">
          {status === "loading" ? (
            <div className="absolute inset-0 flex items-center justify-center bg-white/80 dark:bg-background/80">
              <div className="inline-flex items-center gap-2 rounded-md border border-[#d8dfd8] bg-white px-4 py-3 text-sm font-semibold text-[#334155]/70 shadow-sm dark:border-border dark:bg-card dark:text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                {locale === "zh" ? "正在加载 PDF" : "Loading PDF"}
              </div>
            </div>
          ) : null}
          {isRendering && status === "ready" ? (
            <div className="absolute right-4 top-4 inline-flex items-center gap-2 rounded-md bg-white px-3 py-2 text-xs font-semibold text-[#334155]/65 shadow-sm dark:bg-card dark:text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              {locale === "zh" ? "正在渲染" : "Rendering"}
            </div>
          ) : null}
          <div ref={pageLayerRef} className="relative self-start bg-white shadow-lg">
            <canvas
              ref={canvasRef}
              aria-label={locale === "zh" ? `${title} PDF 第 ${pageNumber} 页` : `${title} PDF page ${pageNumber}`}
              className="block bg-white"
            />
            <div
              ref={textLayerRef}
              aria-label={locale === "zh" ? `${title} PDF 第 ${pageNumber} 页可选择文本` : `${title} selectable PDF text page ${pageNumber}`}
              className="paper-pdf-text-layer absolute left-0 top-0 z-10 overflow-hidden text-transparent"
              onMouseUp={handleTextSelection}
              onKeyUp={handleTextSelection}
            />
            <PdfNoteOverlay notes={currentPageNotes} />
            {selectionDraft && onCreateReaderNote ? (
              <div className="absolute left-3 top-3 z-30 w-[min(22rem,calc(100%-1.5rem))] rounded-md border border-[#d8dfd8] bg-white p-3 text-xs shadow-lg dark:border-border dark:bg-card">
                <div className="font-semibold text-[#334155] dark:text-foreground">
                  {locale === "zh" ? "已选文本" : "Selected text"}
                </div>
                <p className="mt-1 line-clamp-2 leading-5 text-[#334155]/65 dark:text-muted-foreground">
                  {selectionDraft.quote}
                </p>
                <Input
                  aria-label={locale === "zh" ? "批注内容" : "Note text"}
                  className="mt-2 h-8 bg-white dark:bg-background"
                  placeholder={locale === "zh" ? "批注内容" : "Note text"}
                  value={selectionNoteText}
                  onChange={(event) => setSelectionNoteText(event.target.value)}
                />
                <div className="mt-2 flex gap-2">
                  <Button type="button" size="sm" className="h-8 rounded-md" onClick={() => createSelectionNote("highlight")}>
                    {locale === "zh" ? "高亮" : "Highlight"}
                  </Button>
                  <Button type="button" size="sm" variant="outline" className="h-8 rounded-md" onClick={() => createSelectionNote("note")}>
                    {locale === "zh" ? "添加批注" : "Add note"}
                  </Button>
                  <Button type="button" size="sm" variant="ghost" className="h-8 rounded-md" onClick={() => setSelectionDraft(null)}>
                    {locale === "zh" ? "取消" : "Cancel"}
                  </Button>
                </div>
              </div>
            ) : null}
          </div>
          {textLayerStatus === "error" && status === "ready" ? (
            <div className="absolute bottom-4 right-4 rounded-md bg-white px-3 py-2 text-xs font-semibold text-[#334155]/65 shadow-sm dark:bg-card dark:text-muted-foreground">
              {locale === "zh" ? "文本选择不可用" : "Text selection unavailable"}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}

function PdfNoteOverlay({ notes }: { notes: PaperReaderNote[] }) {
  return (
    <div className="pointer-events-none absolute left-0 top-0 z-20 size-full">
      {notes.flatMap((note) =>
        (note.anchor?.rects ?? []).map((rect, index) => (
          <span
            key={`${note.noteId}-${index}`}
            className={[
              "absolute rounded-[2px] border",
              note.kind === "note" ? "border-blue-500/50" : "border-transparent",
              noteColorClass(note.color),
            ].join(" ")}
            title={note.noteText ?? note.quote ?? note.label}
            style={{
              left: `${rect.left}%`,
              top: `${rect.top}%`,
              width: `${rect.width}%`,
              height: `${rect.height}%`,
            }}
          />
        ))
      )}
    </div>
  )
}

function PdfSearchPanel({
  activeIndex,
  locale,
  onQueryChange,
  onSelectResult,
  query,
  results,
  searchStatus,
}: {
  activeIndex: number
  locale: Locale
  onQueryChange: (query: string) => void
  onSelectResult: (index: number) => void
  query: string
  results: PdfSearchResult[]
  searchStatus: "idle" | "indexing" | "ready" | "error"
}) {
  const trimmedQuery = query.trim()
  const queryTooShort = trimmedQuery.length > 0 && trimmedQuery.length < MIN_SEARCH_LENGTH
  const canUseResults = results.length > 0
  const statusText =
    searchStatus === "indexing"
      ? locale === "zh"
        ? "正在索引 PDF 文本"
        : "Indexing PDF text"
      : searchStatus === "error"
        ? locale === "zh"
          ? "PDF 搜索文本不可用。"
          : "PDF search text is unavailable."
        : queryTooShort
          ? locale === "zh"
            ? "至少输入 2 个字符。"
            : "Enter at least 2 characters."
          : trimmedQuery.length >= MIN_SEARCH_LENGTH
            ? locale === "zh"
              ? `${results.length} 个结果`
              : `${results.length} result${results.length === 1 ? "" : "s"}`
            : locale === "zh"
              ? "搜索此 PDF"
              : "Search this PDF"

  return (
    <section className="border-b border-[#d8dfd8] bg-white px-4 py-3 dark:border-border dark:bg-card">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
        <label className="relative min-w-0 flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-[#334155]/45" />
          <Input
            aria-label={locale === "zh" ? "搜索 PDF 文本" : "Search PDF text"}
            className="h-9 bg-white pl-9 dark:bg-background"
            disabled={searchStatus === "indexing"}
            placeholder={locale === "zh" ? "搜索 PDF" : "Search PDF"}
            type="search"
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
          />
        </label>
        <div className="flex items-center gap-2">
          <span className="min-w-36 text-xs font-semibold text-[#334155]/58 dark:text-muted-foreground">
            {statusText}
          </span>
          <Button
            type="button"
            variant="outline"
            size="icon"
            aria-label={locale === "zh" ? "上一个搜索结果" : "Previous search result"}
            disabled={!canUseResults || activeIndex <= 0}
            onClick={() => onSelectResult(activeIndex - 1)}
          >
            <ChevronLeft className="size-4" />
          </Button>
          <Button
            type="button"
            variant="outline"
            size="icon"
            aria-label={locale === "zh" ? "下一个搜索结果" : "Next search result"}
            disabled={!canUseResults || activeIndex >= results.length - 1}
            onClick={() => onSelectResult(activeIndex + 1)}
          >
            <ChevronRight className="size-4" />
          </Button>
        </div>
      </div>
      {canUseResults ? (
        <div className="mt-3 flex gap-2 overflow-x-auto pb-1">
          {results.map((result, index) => (
            <button
              key={result.id}
              type="button"
              aria-current={index === activeIndex ? "true" : undefined}
              className={[
                "min-w-56 max-w-72 rounded-md border px-3 py-2 text-left text-xs leading-5 transition",
                index === activeIndex
                  ? "border-[#315d8a] bg-[#eef4ef] text-[#334155] ring-2 ring-[#315d8a]/20 dark:bg-secondary dark:text-foreground"
                  : "border-[#d8dfd8] bg-white text-[#334155]/68 hover:border-[#315d8a]/60 dark:border-border dark:bg-background dark:text-muted-foreground",
              ].join(" ")}
              onClick={() => onSelectResult(index)}
            >
              <span className="block font-semibold text-[#334155] dark:text-foreground">
                {locale === "zh" ? `第 ${result.pageNumber} 页` : `Page ${result.pageNumber}`}
              </span>
              <span className="line-clamp-2">{result.snippet}</span>
            </button>
          ))}
        </div>
      ) : null}
    </section>
  )
}

function PdfThumbnailRail({
  currentPage,
  locale,
  numPages,
  onSelectPage,
  pdfDocument,
  status,
  title,
}: {
  currentPage: number
  locale: Locale
  numPages: number
  onSelectPage: (pageNumber: number) => void
  pdfDocument: PdfDocument | null
  status: "loading" | "ready" | "error"
  title: string
}) {
  if (status !== "ready" || !pdfDocument || numPages <= 0) {
    return (
      <aside className="hidden border-r border-[#d8dfd8] bg-white p-3 dark:border-border dark:bg-card lg:block">
        <p className="text-xs font-semibold uppercase tracking-[0.16em] text-[#334155]/45 dark:text-muted-foreground">
          {locale === "zh" ? "页面" : "Pages"}
        </p>
      </aside>
    )
  }

  return (
    <aside className="hidden max-h-[78vh] overflow-y-auto border-r border-[#d8dfd8] bg-white p-3 dark:border-border dark:bg-card lg:block">
      <p className="mb-3 text-xs font-semibold uppercase tracking-[0.16em] text-[#334155]/45 dark:text-muted-foreground">
        {locale === "zh" ? "页面" : "Pages"}
      </p>
      <div className="space-y-2">
        {Array.from({ length: numPages }, (_, index) => {
          const thumbnailPage = index + 1
          return (
            <PdfThumbnailButton
              key={thumbnailPage}
              active={thumbnailPage === currentPage}
              onSelect={() => onSelectPage(thumbnailPage)}
              pageNumber={thumbnailPage}
              pdfDocument={pdfDocument}
              locale={locale}
              title={title}
            />
          )
        })}
      </div>
    </aside>
  )
}

function PdfThumbnailButton({
  active,
  onSelect,
  pageNumber,
  pdfDocument,
  locale,
  title,
}: {
  active: boolean
  onSelect: () => void
  pageNumber: number
  pdfDocument: PdfDocument
  locale: Locale
  title: string
}) {
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const renderTaskRef = useRef<PdfRenderTask | null>(null)
  const cancelQueuedRenderRef = useRef<(() => void) | null>(null)
  const renderedForRef = useRef<{ pageNumber: number; pdfDocument: PdfDocument } | null>(null)
  const [isNearViewport, setIsNearViewport] = useState(false)
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading")

  useEffect(() => {
    const target = buttonRef.current
    if (!target || typeof IntersectionObserver === "undefined") {
      setIsNearViewport(true)
      return
    }

    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries.some((entry) => entry.isIntersecting)
        setIsNearViewport(visible)
      },
      { rootMargin: THUMBNAIL_INTERSECTION_ROOT_MARGIN }
    )
    observer.observe(target)

    return () => {
      observer.disconnect()
    }
  }, [])

  useEffect(() => {
    cancelQueuedRenderRef.current?.()
    cancelQueuedRenderRef.current = null
    renderTaskRef.current?.cancel()
    renderTaskRef.current = null

    const alreadyRendered =
      renderedForRef.current?.pdfDocument === pdfDocument && renderedForRef.current.pageNumber === pageNumber

    if (!isNearViewport) {
      if (!alreadyRendered) {
        setStatus("loading")
      }
      return
    }

    if (alreadyRendered) {
      setStatus("ready")
      return
    }

    setStatus("loading")
    const cancelQueuedRender = scheduleThumbnailRender(async ({ isCancelled, setActiveCancel }) => {
      try {
        const page = await pdfDocument.getPage(pageNumber)
        if (isCancelled()) {
          return
        }
        const viewport = page.getViewport({ scale: 1 })
        const thumbnailScale = THUMBNAIL_WIDTH / Math.max(viewport.width, 1)
        const thumbnailViewport = page.getViewport({ scale: thumbnailScale })
        const canvas = canvasRef.current
        const context = canvas?.getContext("2d")
        if (!canvas || !context || isCancelled()) {
          throw new Error("thumbnail canvas context unavailable")
        }
        canvas.width = Math.floor(thumbnailViewport.width)
        canvas.height = Math.floor(thumbnailViewport.height)
        canvas.style.width = `${Math.floor(thumbnailViewport.width)}px`
        canvas.style.height = `${Math.floor(thumbnailViewport.height)}px`
        const renderTask = page.render({ canvasContext: context, viewport: thumbnailViewport })
        renderTaskRef.current = renderTask
        setActiveCancel(() => {
          renderTask.cancel()
          renderTaskRef.current = null
        })
        await renderTask.promise
        if (!isCancelled()) {
          renderedForRef.current = { pageNumber, pdfDocument }
          setStatus("ready")
        }
      } catch {
        if (!isCancelled()) {
          setStatus("error")
        }
      } finally {
        renderTaskRef.current = null
      }
    })
    cancelQueuedRenderRef.current = cancelQueuedRender

    return () => {
      cancelQueuedRenderRef.current?.()
      cancelQueuedRenderRef.current = null
      renderTaskRef.current?.cancel()
      renderTaskRef.current = null
    }
  }, [isNearViewport, pageNumber, pdfDocument])

  return (
    <button
      ref={buttonRef}
      type="button"
      aria-current={active ? "page" : undefined}
      aria-label={locale === "zh" ? `跳转到第 ${pageNumber} 页` : `Go to page ${pageNumber}`}
      className={[
        "block w-full rounded-md border p-1 text-left text-xs transition",
        active
          ? "border-[#315d8a] bg-[#eef4ef] shadow-sm ring-2 ring-[#315d8a]/25 dark:bg-secondary"
          : "border-[#d8dfd8] bg-white hover:border-[#315d8a]/60 dark:border-border dark:bg-background",
      ].join(" ")}
      onClick={onSelect}
    >
      <span className="flex min-h-28 items-center justify-center rounded-sm bg-[#eef4ef] dark:bg-secondary/60">
        {status === "error" ? (
          <span className="px-2 text-center text-[0.68rem] font-semibold text-[#334155]/55 dark:text-muted-foreground">
            {locale === "zh" ? `第 ${pageNumber} 页` : `Page ${pageNumber}`}
          </span>
        ) : null}
        {status === "loading" ? (
          <Loader2 className="size-4 animate-spin text-[#334155]/40" />
        ) : null}
        <canvas
          ref={canvasRef}
          aria-label={locale === "zh" ? `${title} PDF 第 ${pageNumber} 页缩略图` : `${title} PDF thumbnail page ${pageNumber}`}
          className={status === "ready" ? "block bg-white shadow-sm" : "hidden"}
        />
      </span>
      <span className="mt-1 block text-center font-semibold text-[#334155]/65 dark:text-muted-foreground">
        {pageNumber}
      </span>
    </button>
  )
}

function pdfProxyUrl(pdfUrl: string) {
  return `/api/papers/pdf?url=${encodeURIComponent(pdfUrl)}`
}

function renderSimpleTextLayer(container: HTMLElement, content: PdfTextContent) {
  const text = content.items.map((item) => item.str ?? "").filter(Boolean).join(" ")
  const span = document.createElement("span")
  span.textContent = text
  span.style.position = "absolute"
  span.style.left = "0"
  span.style.top = "0"
  span.style.width = "100%"
  span.style.whiteSpace = "pre-wrap"
  container.appendChild(span)
}

function applySearchHighlights(
  container: HTMLElement | null,
  pageNumber: number,
  query: string,
  results: PdfSearchResult[],
  activeIndex: number
) {
  if (!container) {
    return
  }
  const normalizedQuery = query.trim().toLowerCase()
  const matchingPage = normalizedQuery.length >= MIN_SEARCH_LENGTH && results.some((result) => result.pageNumber === pageNumber)
  const activePage = results[activeIndex]?.pageNumber === pageNumber
  let firstMatched = true

  for (const element of Array.from(container.querySelectorAll<HTMLElement>("span, div"))) {
    element.classList.remove("paper-pdf-search-hit", "paper-pdf-search-hit-active")
    if (!matchingPage || !element.textContent?.toLowerCase().includes(normalizedQuery)) {
      continue
    }
    element.classList.add("paper-pdf-search-hit")
    if (activePage && firstMatched) {
      element.classList.add("paper-pdf-search-hit-active")
      firstMatched = false
    }
  }
}

function readCurrentTextSelection(
  pageLayer: HTMLElement | null,
  textLayer: HTMLElement | null,
  pageNumber: number
): PdfTextSelection | null {
  if (!pageLayer || !textLayer || typeof window === "undefined") {
    return null
  }
  const selection = window.getSelection()
  const quote = selection?.toString().trim().replace(/\s+/g, " ")
  if (!selection || !quote || selection.rangeCount <= 0) {
    return null
  }
  const range = selection.getRangeAt(0)
  if (!selectionIntersectsNode(range, textLayer)) {
    return null
  }
  const layerRect = pageLayer.getBoundingClientRect()
  const rects = Array.from(range.getClientRects())
    .map((rect) => toRelativeRect(rect, layerRect))
    .filter((rect) => rect.width > 0 && rect.height > 0)
    .slice(0, 20)
  return {
    pageNumber,
    quote: quote.slice(0, 2000),
    rects: rects.length ? rects : [{ left: 0, top: 0, width: 100, height: 3 }],
  }
}

function selectionIntersectsNode(range: Range, node: Node) {
  if (typeof range.intersectsNode === "function") {
    return range.intersectsNode(node)
  }
  const container = range.commonAncestorContainer
  return node === container || node.contains(container)
}

function toRelativeRect(rect: DOMRect, layerRect: DOMRect): PaperReaderNoteRect {
  const width = Math.max(layerRect.width, 1)
  const height = Math.max(layerRect.height, 1)
  return {
    left: clampPercent(((rect.left - layerRect.left) / width) * 100),
    top: clampPercent(((rect.top - layerRect.top) / height) * 100),
    width: clampPercent((rect.width / width) * 100),
    height: clampPercent((rect.height / height) * 100),
  }
}

function clearWindowSelection() {
  if (typeof window !== "undefined") {
    window.getSelection()?.removeAllRanges()
  }
}

function noteColorClass(color: PaperReaderNote["color"]) {
  switch (color) {
    case "green":
      return "bg-emerald-300/35"
    case "blue":
      return "bg-sky-300/35"
    case "pink":
      return "bg-rose-300/35"
    case "yellow":
    default:
      return "bg-yellow-300/40"
  }
}

function searchCacheKey({ paperId, pdfUrl }: { paperId?: string; pdfUrl: string }) {
  const stableId = paperId?.trim() || `pdf-${stableHash(pdfUrl)}`
  return `newsroom:paper-pdf-search:v1:${encodeURIComponent(stableId)}`
}

function readSearchCache(cacheKey: string, numPages: number): PdfPageText[] | null {
  if (typeof window === "undefined") {
    return null
  }
  try {
    const raw = window.localStorage.getItem(cacheKey)
    if (!raw || raw.length > SEARCH_CACHE_MAX_BYTES) {
      return null
    }
    const payload = JSON.parse(raw) as { createdAt?: number; numPages?: number; pageTexts?: PdfPageText[] }
    if (!payload.createdAt || Date.now() - payload.createdAt > SEARCH_CACHE_TTL_MS || payload.numPages !== numPages) {
      return null
    }
    if (!Array.isArray(payload.pageTexts) || payload.pageTexts.length !== numPages) {
      return null
    }
    return payload.pageTexts.map((page) => ({
      pageNumber: Number(page.pageNumber),
      text: String(page.text ?? ""),
    }))
  } catch {
    return null
  }
}

function writeSearchCache(cacheKey: string, pageTexts: PdfPageText[]) {
  if (typeof window === "undefined") {
    return
  }
  try {
    const payload = JSON.stringify({
      createdAt: Date.now(),
      numPages: pageTexts.length,
      pageTexts,
    })
    if (payload.length > SEARCH_CACHE_MAX_BYTES) {
      return
    }
    window.localStorage.setItem(cacheKey, payload)
    updateSearchCacheIndex(cacheKey)
  } catch {
    // Browser storage is best-effort only.
  }
}

function updateSearchCacheIndex(cacheKey: string) {
  try {
    const rawIndex = window.localStorage.getItem(SEARCH_CACHE_INDEX_KEY)
    const index = rawIndex ? (JSON.parse(rawIndex) as Array<{ key: string; updatedAt: number }>) : []
    const nextIndex = [
      { key: cacheKey, updatedAt: Date.now() },
      ...index.filter((item) => item.key !== cacheKey),
    ].slice(0, SEARCH_CACHE_MAX_ITEMS)
    for (const stale of index.slice(SEARCH_CACHE_MAX_ITEMS - 1)) {
      if (stale.key !== cacheKey) {
        window.localStorage.removeItem(stale.key)
      }
    }
    window.localStorage.setItem(SEARCH_CACHE_INDEX_KEY, JSON.stringify(nextIndex))
  } catch {
    // Ignore local cache pruning failures.
  }
}

function stableHash(value: string) {
  let hash = 5381
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 33) ^ value.charCodeAt(index)
  }
  return (hash >>> 0).toString(36)
}

function clampPercent(value: number) {
  if (!Number.isFinite(value)) {
    return 0
  }
  return Math.min(Math.max(Number(value.toFixed(4)), 0), 100)
}

function scheduleThumbnailRender(run: ThumbnailRenderJob["run"]) {
  const job: ThumbnailRenderJob = {
    cancelled: false,
    run,
    started: false,
  }
  thumbnailRenderQueue.push(job)
  drainThumbnailRenderQueue()

  return () => {
    job.cancelled = true
    job.activeCancel?.()
    if (!job.started) {
      const index = thumbnailRenderQueue.indexOf(job)
      if (index >= 0) {
        thumbnailRenderQueue.splice(index, 1)
      }
    }
    drainThumbnailRenderQueue()
  }
}

function drainThumbnailRenderQueue() {
  while (activeThumbnailRenderJobs < MAX_THUMBNAIL_RENDER_CONCURRENCY && thumbnailRenderQueue.length > 0) {
    const job = thumbnailRenderQueue.shift()
    if (!job || job.cancelled) {
      continue
    }
    job.started = true
    activeThumbnailRenderJobs += 1
    void (async () => {
      try {
        await job.run({
          isCancelled: () => job.cancelled,
          setActiveCancel: (cancel) => {
            job.activeCancel = cancel
          },
        })
      } finally {
        activeThumbnailRenderJobs = Math.max(0, activeThumbnailRenderJobs - 1)
        job.activeCancel = undefined
        drainThumbnailRenderQueue()
      }
    })()
  }
}

function buildSearchResults(pageTexts: PdfPageText[], query: string): PdfSearchResult[] {
  const normalizedQuery = query.trim().toLowerCase()
  if (normalizedQuery.length < MIN_SEARCH_LENGTH) {
    return []
  }
  const results: PdfSearchResult[] = []
  for (const page of pageTexts) {
    const normalizedText = page.text.toLowerCase()
    const matchIndex = normalizedText.indexOf(normalizedQuery)
    if (matchIndex < 0) {
      continue
    }
    results.push({
      id: `${page.pageNumber}:${matchIndex}`,
      pageNumber: page.pageNumber,
      matchIndex,
      snippet: buildSnippet(page.text, matchIndex, query.trim().length),
    })
  }
  return results
}

function buildSnippet(text: string, matchIndex: number, queryLength: number) {
  const start = Math.max(0, matchIndex - 42)
  const end = Math.min(text.length, matchIndex + queryLength + 58)
  const prefix = start > 0 ? "... " : ""
  const suffix = end < text.length ? " ..." : ""
  return `${prefix}${text.slice(start, end).replace(/\s+/g, " ").trim()}${suffix}`
}

function clampSearchIndex(value: number, resultCount: number) {
  if (!Number.isFinite(value) || resultCount <= 0) {
    return 0
  }
  return Math.min(Math.max(value, 0), resultCount - 1)
}

function clampPage(value: number, numPages: number) {
  if (!Number.isFinite(value)) {
    return 1
  }
  return Math.min(Math.max(value, 1), Math.max(numPages, 1))
}

function clampScale(value: number) {
  if (!Number.isFinite(value)) {
    return 1
  }
  return Math.min(Math.max(Number(value.toFixed(2)), MIN_SCALE), MAX_SCALE)
}
