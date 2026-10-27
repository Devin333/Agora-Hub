import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { PaperPdfViewer } from "@/components/papers/shared/paper-pdf-viewer"

const pdfjsMock = vi.hoisted(() => {
  const GlobalWorkerOptions = { workerSrc: "" }
  const renderCancel = vi.fn()
  const thumbnailRenderStarts: number[] = []
  const thumbnailRenderCancels: number[] = []
  const pendingThumbnailRenders: Array<{ pageNumber: number; resolve: () => void }> = []
  const textLayerRender = vi.fn()
  let activeThumbnailRenderCount = 0
  let holdThumbnailRenders = false
  let maxActiveThumbnailRenderCount = 0
  let textLayerFailure = false
  const finishThumbnailRender = (done: { current: boolean }, resolve: () => void) => {
    if (done.current) {
      return
    }
    done.current = true
    activeThumbnailRenderCount = Math.max(0, activeThumbnailRenderCount - 1)
    resolve()
  }
  const render = vi.fn((pageNumber: number, params: { viewport: { width: number } }) => {
    const isThumbnail = params.viewport.width <= 120
    if (!isThumbnail) {
      return { promise: Promise.resolve(), cancel: renderCancel }
    }

    thumbnailRenderStarts.push(pageNumber)
    activeThumbnailRenderCount += 1
    maxActiveThumbnailRenderCount = Math.max(maxActiveThumbnailRenderCount, activeThumbnailRenderCount)
    const done = { current: false }
    let resolvePromise: () => void = () => undefined
    const promise = new Promise<void>((resolve) => {
      resolvePromise = resolve
    })
    const resolveThumbnail = () => finishThumbnailRender(done, resolvePromise)
    if (holdThumbnailRenders) {
      pendingThumbnailRenders.push({ pageNumber, resolve: resolveThumbnail })
    } else {
      queueMicrotask(resolveThumbnail)
    }
    const cancel = vi.fn(() => {
      renderCancel()
      thumbnailRenderCancels.push(pageNumber)
      resolveThumbnail()
    })
    return { promise, cancel }
  })
  const getViewport = vi.fn(({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale }))
  const getTextContent = vi.fn(async (pageNumber: number) => ({
    items: (pageTexts.get(pageNumber) ?? "").split(/\s+/).filter(Boolean).map((str) => ({ str }))
  }))
  const defaultPageTexts = new Map([
    [1, "Diffusion model explains attention memory and document planning."],
    [2, "Latent Transformer benchmark improves retrieval and long context reading."],
    [3, "Engineering notes mention LATENT planning and benchmark limits."]
  ])
  const pageTexts = new Map(defaultPageTexts)
  let textContentFailure = false
  const createPage = (pageNumber: number) => ({
    getTextContent: vi.fn(async () => {
      if (textContentFailure) {
        throw new Error("text extraction failed")
      }
      return getTextContent(pageNumber)
    }),
    getViewport,
    render: vi.fn((params: { viewport: { width: number } }) => render(pageNumber, params))
  })
  const page = {
    getTextContent: vi.fn(async () => getTextContent(1)),
    getViewport,
    render: vi.fn((params: { viewport: { width: number } }) => render(1, params))
  }
  const pdf = {
    numPages: 3,
    getPage: vi.fn(async (pageNumber: number) => {
      return createPage(pageNumber)
    }),
    destroy: vi.fn(async () => undefined)
  }
  const getDocument = vi.fn()
  let resolveDocument: (value: typeof pdf) => void = () => undefined
  let rejectDocument: (reason: Error) => void = () => undefined
  const loadingDestroy = vi.fn()
  class TextLayer {
    readonly container: HTMLElement
    readonly textContentSource: { items: Array<{ str?: string }> }

    constructor({
      container,
      textContentSource
    }: {
      container: HTMLElement
      textContentSource: { items: Array<{ str?: string }> }
    }) {
      this.container = container
      this.textContentSource = textContentSource
    }

    cancel = vi.fn()

    render = vi.fn(async () => {
      textLayerRender()
      if (textLayerFailure) {
        throw new Error("text layer failed")
      }
      this.container.replaceChildren()
      for (const item of this.textContentSource.items) {
        if (!item.str) {
          continue
        }
        const span = document.createElement("span")
        span.textContent = item.str
        span.style.position = "absolute"
        this.container.appendChild(span)
      }
    })
  }

  return {
    GlobalWorkerOptions,
    getDocument,
    getTextContent,
    loadingDestroy,
    page,
    pdf,
    pendingThumbnailRenders,
    render,
    renderCancel,
    TextLayer,
    textLayerRender,
    thumbnailRenderCancels,
    thumbnailRenderStarts,
    resolveDocument: () => resolveDocument(pdf),
    rejectDocument: () => rejectDocument(new Error("pdf failed")),
    reset: () => {
      GlobalWorkerOptions.workerSrc = ""
      renderCancel.mockReset()
      render.mockClear()
      thumbnailRenderStarts.length = 0
      thumbnailRenderCancels.length = 0
      pendingThumbnailRenders.length = 0
      activeThumbnailRenderCount = 0
      holdThumbnailRenders = false
      maxActiveThumbnailRenderCount = 0
      getViewport.mockClear()
      getTextContent.mockClear()
      textLayerRender.mockClear()
      page.getTextContent.mockClear()
      page.getViewport.mockClear()
      pageTexts.clear()
      for (const [pageNumber, text] of defaultPageTexts.entries()) {
        pageTexts.set(pageNumber, text)
      }
      textContentFailure = false
      textLayerFailure = false
      pdf.getPage.mockReset()
      pdf.getPage.mockImplementation(async (pageNumber: number) => {
        return createPage(pageNumber)
      })
      pdf.destroy.mockClear()
      loadingDestroy.mockReset()
      getDocument.mockReset()
      getDocument.mockImplementation(() => ({
        promise: new Promise<typeof pdf>((resolve, reject) => {
          resolveDocument = resolve
          rejectDocument = reject
        }),
        destroy: loadingDestroy
      }))
    },
    createPage,
    failTextContent: () => {
      textContentFailure = true
    },
    failTextLayer: () => {
      textLayerFailure = true
    },
    getMaxActiveThumbnailRenderCount: () => maxActiveThumbnailRenderCount,
    holdThumbnailRenders: () => {
      holdThumbnailRenders = true
    },
    releaseNextThumbnailRender: () => {
      pendingThumbnailRenders.shift()?.resolve()
    },
    setPageText: (pageNumber: number, text: string) => {
      pageTexts.set(pageNumber, text)
    }
  }
})

vi.mock("pdfjs-dist", () => ({
  default: {
    GlobalWorkerOptions: pdfjsMock.GlobalWorkerOptions,
    getDocument: pdfjsMock.getDocument,
    TextLayer: pdfjsMock.TextLayer
  },
  GlobalWorkerOptions: pdfjsMock.GlobalWorkerOptions,
  getDocument: pdfjsMock.getDocument,
  TextLayer: pdfjsMock.TextLayer
}))

function renderViewer(props: Partial<Parameters<typeof PaperPdfViewer>[0]> = {}) {
  return render(
    <PaperPdfViewer
      pdfUrl="https://arxiv.org/pdf/2605.00001.pdf"
      title="Reader Paper"
      locale="en"
      fallback={<div>Text fallback content</div>}
      {...props}
    />
  )
}

function installIntersectionObserverMock() {
  const previousIntersectionObserver = globalThis.IntersectionObserver
  const instances: MockIntersectionObserver[] = []

  class MockIntersectionObserver {
    readonly callback: IntersectionObserverCallback
    readonly options?: IntersectionObserverInit
    readonly targets = new Set<Element>()

    constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
      this.callback = callback
      this.options = options
      instances.push(this)
    }

    disconnect = vi.fn(() => {
      this.targets.clear()
    })

    observe = vi.fn((target: Element) => {
      this.targets.add(target)
    })

    takeRecords = vi.fn(() => [])

    unobserve = vi.fn((target: Element) => {
      this.targets.delete(target)
    })
  }

  Object.defineProperty(globalThis, "IntersectionObserver", {
    configurable: true,
    value: MockIntersectionObserver,
    writable: true
  })

  return {
    getInstances: () => instances,
    restore: () => {
      if (previousIntersectionObserver) {
        Object.defineProperty(globalThis, "IntersectionObserver", {
          configurable: true,
          value: previousIntersectionObserver,
          writable: true
        })
      } else {
        Reflect.deleteProperty(globalThis, "IntersectionObserver")
      }
    },
    trigger: (target: Element, isIntersecting: boolean) => {
      for (const instance of instances) {
        if (!instance.targets.has(target)) {
          continue
        }
        instance.callback(
          [
            {
              isIntersecting,
              target
            } as IntersectionObserverEntry
          ],
          instance as unknown as IntersectionObserver
        )
      }
    }
  }
}

async function resolveViewerDocument() {
  await waitFor(() => expect(pdfjsMock.getDocument).toHaveBeenCalled())
  await act(async () => {
    pdfjsMock.resolveDocument()
  })
  expect(await screen.findByText("Page 1 of 3")).toBeInTheDocument()
}

function installSelectionMock(quote = "Diffusion model") {
  const previousGetSelection = window.getSelection
  const range = {
    commonAncestorContainer: document.body,
    getClientRects: () => [
      {
        bottom: 56,
        height: 16,
        left: 60,
        right: 220,
        top: 40,
        width: 160,
        x: 60,
        y: 40,
        toJSON: () => ({})
      } as DOMRect
    ],
    intersectsNode: () => true
  }
  Object.defineProperty(window, "getSelection", {
    configurable: true,
    value: () => ({
      rangeCount: 1,
      getRangeAt: () => range,
      removeAllRanges: vi.fn(),
      toString: () => quote
    })
  })

  return () => {
    Object.defineProperty(window, "getSelection", {
      configurable: true,
      value: previousGetSelection
    })
  }
}

describe("PaperPdfViewer", () => {
  beforeEach(() => {
    pdfjsMock.reset()
    window.localStorage.clear()
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      setTransform: vi.fn()
    } as unknown as CanvasRenderingContext2D)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    Reflect.deleteProperty(globalThis, "IntersectionObserver")
  })

  it("renders the default chrome on design tokens with floating controls and token active states", async () => {
    const { container } = renderViewer()
    const viewer = screen.getByLabelText("Reader Paper PDF viewer")
    expect(viewer).toHaveAttribute("data-pdf-appearance", "default")
    expect(viewer).toHaveClass("bg-background", "text-foreground", "focus-visible:ring-ring/50")
    expect(container.querySelector("[data-pdf-toolbar]")).toHaveClass("border-border", "bg-background")
    // Layout groups are transparent in the default Portal flex toolbar.
    expect(container.querySelector("[data-pdf-pagination]")).toHaveClass("contents")
    expect(container.querySelector("[data-pdf-zoom]")).toHaveClass("contents")
    expect(container.querySelector("[data-pdf-page-flow]")).toHaveClass("contents")
    expect(container.querySelector("[data-pdf-canvas-scroll]")).toHaveClass("rk-scroll", "bg-muted")
    expect(screen.getByText("Reader Paper")).toHaveClass("text-foreground")
    expect(screen.getByRole("button", { name: "Next page" })).toHaveClass("rounded-full", "bg-muted/90")
    await resolveViewerDocument()
    expect(screen.getByRole("button", { name: "Go to page 1" })).toHaveClass("border-ring", "bg-accent", "ring-ring/25")
    await waitFor(() => expect(pdfjsMock.getTextContent).toHaveBeenCalledWith(3))
    fireEvent.change(screen.getByLabelText("Search PDF text"), { target: { value: "latent" } })
    await screen.findByText("2 results")
    expect(container.querySelector('[data-pdf-search-result][aria-current="true"]')).toHaveClass("border-ring", "bg-accent", "ring-ring/20")
  })

  it("exposes opt-in chrome slots in loading, controls, thumbnails, and search without altering document pixels", async () => {
    const { container } = renderViewer({ appearance: "paper-agent", className: "agent-pdf" })
    const viewer = screen.getByLabelText("Reader Paper PDF viewer")
    expect(viewer).toHaveAttribute("data-pdf-appearance", "paper-agent")
    expect(viewer).toHaveClass("agent-pdf")
    for (const slot of ["toolbar", "search", "thumbnails", "canvas-scroll", "divider", "text", "muted", "loading-overlay", "message"]) {
      expect(viewer.querySelector('[data-pdf-' + slot + ']')).not.toBeNull()
    }
    for (const control of viewer.querySelectorAll("button, input")) expect(control).toHaveAttribute("data-pdf-control")
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled()
    expect(screen.getByLabelText("Page number")).toBeDisabled()
    await resolveViewerDocument()
    expect(screen.getByRole("button", { name: "Next page" })).not.toBeDisabled()
    const thumbnail = screen.getByRole("button", { name: "Go to page 1" })
    expect(thumbnail).toHaveAttribute("data-pdf-thumbnail")
    expect(thumbnail).toHaveAttribute("data-pdf-control", "navigation")
    expect(thumbnail).toHaveAttribute("aria-current", "page")
    expect(thumbnail.querySelector("[data-pdf-thumbnail-viewport]")).not.toBeNull()
    const pageCanvas = screen.getByLabelText("Reader Paper PDF page 1")
    expect(pageCanvas).toHaveClass("block", "bg-white")
    expect(pageCanvas.parentElement).toHaveAttribute("data-pdf-document")
    await waitFor(() => expect(pdfjsMock.getTextContent).toHaveBeenCalledWith(3))
    fireEvent.change(screen.getByLabelText("Search PDF text"), { target: { value: "latent" } })
    await screen.findByText("2 results")
    const results = container.querySelectorAll("[data-pdf-search-result]")
    expect(results).toHaveLength(2)
    for (const result of results) expect(result).toHaveAttribute("data-pdf-control", "navigation")
    expect(results[0]).toHaveAttribute("aria-current", "true")
    const thirdPageResult = screen.getByText(/Engineering notes mention LATENT planning/i)
    fireEvent.click(thirdPageResult.closest("button")!)
    expect(await screen.findByText("Page 3 of 3")).toBeInTheDocument()
    for (const canvas of container.querySelectorAll("canvas")) {
      expect(canvas.style.filter).toBe("")
      expect(canvas).not.toHaveAttribute("data-pdf-control")
      expect(canvas).not.toHaveAttribute("data-pdf-muted")
    }
    expect(container.querySelector("[data-pdf-text-layer]")).toHaveClass("text-transparent")
  })

  it.each(["default", "paper-agent"] as const)("isolates %s page placement from document and annotation coordinates", async (appearance) => {
    const { container } = renderViewer({ appearance, className: "agent-pdf" })
    const scroll = container.querySelector("[data-pdf-canvas-scroll]")!
    const flow = container.querySelector("[data-pdf-page-flow]")!
    const documentPage = container.querySelector<HTMLElement>("[data-pdf-document]")!
    expect(flow.parentElement).toBe(scroll)
    expect(flow.children).toHaveLength(1)
    expect(flow.firstElementChild).toBe(documentPage)
    expect(flow).not.toHaveAttribute("data-pdf-document")
    expect(documentPage).not.toHaveAttribute("data-pdf-page-flow")
    expect(documentPage).toHaveClass("relative", "self-start", "bg-white", "shadow-lg")
    expect(documentPage).not.toHaveClass("contents")
    expect(documentPage.style.cssText).toBe("")
    if (appearance === "default") expect(flow).toHaveClass("contents")
    else expect(flow).not.toHaveClass("contents")
    await resolveViewerDocument()
    const canvas = screen.getByLabelText("Reader Paper PDF page 1")
    expect(canvas.parentElement).toBe(documentPage)
    expect(container.querySelector("[data-pdf-text-layer]")?.parentElement).toBe(documentPage)
    expect(documentPage.style.cssText).toBe("")
  })

  it("groups opt-in pagination, zoom and search without changing their user actions", async () => {
    const title = "Long-unbroken-paper-source-".repeat(8)
    const { container } = renderViewer({ title, appearance: "paper-agent", className: "agent-pdf" })
    const pagination = container.querySelector("[data-pdf-pagination]")!
    const zoom = container.querySelector("[data-pdf-zoom]")!
    const searchRow = container.querySelector("[data-pdf-search-row]")!
    expect(container.querySelector("[data-pdf-title] h2")).toHaveTextContent(title)
    expect(pagination).not.toHaveClass("contents")
    expect(zoom).not.toHaveClass("contents")
    expect(pagination).toContainElement(screen.getByRole("button", { name: "Previous page" }))
    expect(pagination).toContainElement(screen.getByRole("button", { name: "Next page" }))
    expect(pagination.querySelector("[data-pdf-page-form]")).toContainElement(screen.getByLabelText("Page number"))
    expect(zoom).toContainElement(screen.getByRole("button", { name: "Zoom out" }))
    expect(zoom).toContainElement(screen.getByRole("button", { name: "Zoom in" }))
    expect(zoom.querySelector("[data-pdf-zoom-value]")).toHaveTextContent("100%")
    expect(searchRow.querySelector("[data-pdf-search-field]")).toContainElement(screen.getByLabelText("Search PDF text"))
    const searchNavigation = searchRow.querySelector("[data-pdf-search-navigation]")!
    expect(searchNavigation).toContainElement(screen.getByRole("button", { name: "Previous search result" }))
    expect(searchNavigation).toContainElement(screen.getByRole("button", { name: "Next search result" }))
    expect(searchNavigation.querySelector("[data-pdf-search-status]")).not.toBeNull()

    await resolveViewerDocument()
    fireEvent.change(screen.getByLabelText("Page number"), { target: { value: "2" } })
    fireEvent.submit(container.querySelector("[data-pdf-page-form]")!)
    expect(await screen.findByText("Page 2 of 3")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }))
    expect(zoom.querySelector("[data-pdf-zoom-value]")).toHaveTextContent("125%")
    fireEvent.click(screen.getByRole("button", { name: "Zoom out" }))
    expect(zoom.querySelector("[data-pdf-zoom-value]")).toHaveTextContent("100%")
    await waitFor(() => expect(pdfjsMock.getTextContent).toHaveBeenCalledWith(3))
    fireEvent.change(screen.getByLabelText("Search PDF text"), { target: { value: "latent" } })
    await screen.findByText("2 results")
    expect(container.querySelector("[data-pdf-search-results]")?.querySelectorAll("[data-pdf-search-result]")).toHaveLength(2)
    fireEvent.click(screen.getByRole("button", { name: "Next search result" }))
    expect(await screen.findByText("Page 3 of 3")).toBeInTheDocument()
  })

  it("retains the opt-in appearance and class on real PDF load failure", async () => {
    const { container } = renderViewer({ appearance: "paper-agent", className: "agent-pdf" })
    await waitFor(() => expect(pdfjsMock.getDocument).toHaveBeenCalled())
    await act(async () => pdfjsMock.rejectDocument())
    await screen.findByText("PDF could not be rendered; showing text fallback.")
    expect(container.querySelector('[data-pdf-appearance="paper-agent"]')).toHaveClass("agent-pdf")
    expect(container.querySelector("[data-pdf-failure]")).toBeInTheDocument()
    expect(screen.getByText("Text fallback content")).toBeInTheDocument()
    expect(container.querySelector("[data-pdf-canvas-scroll]")).toBeNull()
  })

  it("shows loading state and loads only through the PDF proxy", async () => {
    renderViewer()

    expect(screen.getAllByText("Loading PDF")).toHaveLength(2)
    await waitFor(() => expect(pdfjsMock.getDocument).toHaveBeenCalled())
    expect(pdfjsMock.GlobalWorkerOptions.workerSrc).toBe("/pdf.worker.min.mjs")
    expect(pdfjsMock.getDocument).toHaveBeenCalledWith(
      expect.objectContaining({
        url: "/api/papers/pdf?url=https%3A%2F%2Farxiv.org%2Fpdf%2F2605.00001.pdf"
      })
    )
    expect(pdfjsMock.getDocument).not.toHaveBeenCalledWith(
      expect.objectContaining({ url: "https://arxiv.org/pdf/2605.00001.pdf" })
    )
  })

  it.each([
    ["J v2 exact source", "paper_c9aa5a22011b", "sha256:" + "b".repeat(64), "sha256:" + "a".repeat(64)],
    ["encoded identity binding", "paper%3Aowned", "sha256%3A" + "b".repeat(64), "sha256%3A" + "a".repeat(64)],
  ])("loads the %s directly through the same-origin private raw route", async (_label, paperId, revision, fingerprint) => {
    const pdfUrl = '/api/v1/research/desktop/papers/' + paperId + '/source?sourceRevision=' + revision + '&sourceFingerprint=' + fingerprint
    const onPageChange = vi.fn()
    renderViewer({ pdfUrl, appearance: "paper-agent", onPageChange })
    await resolveViewerDocument()
    expect(pdfjsMock.getDocument).toHaveBeenCalledOnce()
    expect(pdfjsMock.getDocument).toHaveBeenCalledWith({
      disableAutoFetch: true,
      disableStream: true,
      rangeChunkSize: 65536,
      url: pdfUrl,
      withCredentials: false,
    })
    await waitFor(() => expect(onPageChange).toHaveBeenCalledWith(1, 3))
    expect(screen.getByLabelText("Reader Paper PDF page 1")).toBeInTheDocument()
    expect(screen.queryByText("Text fallback content")).not.toBeInTheDocument()
  })

  it("loads a changed private source binding anew rather than reusing an older source", async () => {
    const fingerprint = 'sha256:' + 'a'.repeat(64)
    const oldUrl = '/api/v1/research/desktop/papers/paper_c9aa5a22011b/source?sourceRevision=revision-1&sourceFingerprint=' + fingerprint
    const newUrl = '/api/v1/research/desktop/papers/paper_c9aa5a22011b/source?sourceRevision=revision-2&sourceFingerprint=' + fingerprint
    const { rerender } = renderViewer({ pdfUrl: oldUrl })
    await resolveViewerDocument()
    const getDocumentCalls = pdfjsMock.getDocument.mock.calls.length
    rerender(<PaperPdfViewer pdfUrl={newUrl} title="Reader Paper" locale="en" fallback={<div>Text fallback content</div>} />)
    await waitFor(() => expect(pdfjsMock.getDocument).toHaveBeenCalledTimes(getDocumentCalls + 1))
    expect(pdfjsMock.pdf.destroy).toHaveBeenCalledOnce()
    expect(pdfjsMock.loadingDestroy).toHaveBeenCalledOnce()
    expect(pdfjsMock.getDocument).toHaveBeenLastCalledWith(expect.objectContaining({ url: newUrl, withCredentials: false }))
    await act(async () => pdfjsMock.resolveDocument())
    expect(await screen.findByText("Page 1 of 3")).toBeInTheDocument()
  })

  it.each([
    ["missing revision", "/api/v1/research/desktop/papers/paper-1/source?sourceFingerprint=FINGERPRINT"],
    ["empty revision", "/api/v1/research/desktop/papers/paper-1/source?sourceRevision=&sourceFingerprint=FINGERPRINT"],
    ["oversize revision", "/api/v1/research/desktop/papers/paper-1/source?sourceRevision=" + "x".repeat(513) + "&sourceFingerprint=FINGERPRINT"],
    ["missing fingerprint", "/api/v1/research/desktop/papers/paper-1/source?sourceRevision=revision-1"],
    ["invalid fingerprint", "/api/v1/research/desktop/papers/paper-1/source?sourceRevision=revision-1&sourceFingerprint=sha256:bad"],
    ["duplicate revision", "/api/v1/research/desktop/papers/paper-1/source?sourceRevision=one&sourceRevision=two&sourceFingerprint=FINGERPRINT"],
    ["duplicate fingerprint", "/api/v1/research/desktop/papers/paper-1/source?sourceRevision=one&sourceFingerprint=FINGERPRINT&sourceFingerprint=FINGERPRINT"],
    ["unknown query", "/api/v1/research/desktop/papers/paper-1/source?sourceRevision=one&sourceFingerprint=FINGERPRINT&userId=owner-1"],
    ["encoded duplicate revision", "/api/v1/research/desktop/papers/paper-1/source?sourceRevision=one&%73ourceRevision=two&sourceFingerprint=FINGERPRINT"],
    ["fragment", "/api/v1/research/desktop/papers/paper-1/source?sourceRevision=one&sourceFingerprint=FINGERPRINT#page=2"],
    ["empty fragment", "/api/v1/research/desktop/papers/paper-1/source?sourceRevision=one&sourceFingerprint=FINGERPRINT#"],
    ["arbitrary relative path", "/api/admin/secrets?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["non-root relative path", "api/v1/research/desktop/papers/paper-1/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["protocol-relative host", "//evil.test/api/v1/research/desktop/papers/paper-1/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["absolute private host", "https://evil.test/api/v1/research/desktop/papers/paper-1/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["remote loopback private host", "http://127.0.0.1:8124/api/v1/research/desktop/papers/paper-1/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["extra path segment", "/api/v1/research/desktop/papers/paper-1/other/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["dot segment", "/api/v1/research/desktop/papers/../papers/paper-1/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["encoded traversal", "/api/v1/research/desktop/papers/%2e%2e/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["embedded traversal", "/api/v1/research/desktop/papers/paper..1/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["encoded slash", "/api/v1/research/desktop/papers/paper%2F1/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["double encoded slash", "/api/v1/research/desktop/papers/paper%252F1/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["encoded backslash", "/api/v1/research/desktop/papers/paper%5C1/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["malformed encoding", "/api/v1/research/desktop/papers/paper%GG/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["invalid UTF-8 encoding", "/api/v1/research/desktop/papers/paper%C0%AF/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["encoded revision controls", "/api/v1/research/desktop/papers/paper-1/source?sourceRevision=one%00two&sourceFingerprint=FINGERPRINT"],
    ["oversize paper ID", "/api/v1/research/desktop/papers/" + "x".repeat(257) + "/source?sourceRevision=one&sourceFingerprint=FINGERPRINT"],
    ["filesystem path", "file:///F:/private/paper.pdf"],
    ["public URL credentials", "https://user:secret@arxiv.org/pdf/2605.00001.pdf"],
  ])("rejects %s before any PDF transport or proxy request", async (_label, input) => {
    const onLoadError = vi.fn()
    const onPageChange = vi.fn()
    const pdfUrl = input.replaceAll("FINGERPRINT", 'sha256:' + 'a'.repeat(64))
    renderViewer({ pdfUrl, onLoadError, onPageChange })
    expect(await screen.findByText("Text fallback content")).toBeInTheDocument()
    expect(onLoadError).toHaveBeenCalledOnce()
    expect(onPageChange).not.toHaveBeenCalled()
    expect(pdfjsMock.getDocument).not.toHaveBeenCalled()
    expect(pdfjsMock.pdf.getPage).not.toHaveBeenCalled()
  })

  it.each(["http://arxiv.org/pdf/2605.00001.pdf", "https://arxiv.org/pdf/2605.00001.pdf"])("keeps public HTTP PDFs behind the existing proxy: %s", async pdfUrl => {
    renderViewer({ pdfUrl })
    await resolveViewerDocument()
    expect(pdfjsMock.getDocument).toHaveBeenCalledWith({
      disableAutoFetch: true,
      disableStream: true,
      rangeChunkSize: 65536,
      url: '/api/papers/pdf?url=' + encodeURIComponent(pdfUrl),
      withCredentials: false,
    })
  })

  it("uses the real viewer failure callback/fallback if a private PDF load is denied", async () => {
    const pdfUrl = '/api/v1/research/desktop/papers/paper_c9aa5a22011b/source?sourceRevision=sha256:' + 'b'.repeat(64) + '&sourceFingerprint=sha256:' + 'a'.repeat(64)
    const onLoadError = vi.fn()
    const onPageChange = vi.fn()
    renderViewer({ pdfUrl, onLoadError, onPageChange, appearance: "paper-agent" })
    await waitFor(() => expect(pdfjsMock.getDocument).toHaveBeenCalledWith(expect.objectContaining({ url: pdfUrl })))
    await act(async () => pdfjsMock.rejectDocument())
    expect(await screen.findByText("Text fallback content")).toBeInTheDocument()
    expect(onLoadError).toHaveBeenCalledOnce()
    expect(onPageChange).not.toHaveBeenCalled()
    expect(screen.queryByText("Page 1 of 3")).not.toBeInTheDocument()
  })

  it("renders page count when the document resolves", async () => {
    renderViewer()

    await resolveViewerDocument()

    expect(await screen.findByText("Page 1 of 3")).toBeInTheDocument()
    expect(screen.getByText("/ 3")).toBeInTheDocument()
    await waitFor(() => expect(pdfjsMock.pdf.getPage).toHaveBeenCalledWith(1))
  })

  it("restores and clamps the initial page", async () => {
    const onPageChange = vi.fn()
    const { rerender } = renderViewer({ initialPage: 2, onPageChange })

    await waitFor(() => expect(pdfjsMock.getDocument).toHaveBeenCalled())
    await act(async () => {
      pdfjsMock.resolveDocument()
    })

    expect(await screen.findByText("Page 2 of 3")).toBeInTheDocument()
    await waitFor(() => expect(onPageChange).toHaveBeenCalledWith(2, 3))

    pdfjsMock.reset()
    rerender(
      <PaperPdfViewer
        pdfUrl="https://arxiv.org/pdf/2605.00001.pdf?reload=1"
        title="Reader Paper"
        locale="en"
        fallback={<div>Text fallback content</div>}
        initialPage={99}
      />
    )

    await waitFor(() => expect(pdfjsMock.getDocument).toHaveBeenCalled())
    await act(async () => {
      pdfjsMock.resolveDocument()
    })
    expect(await screen.findByText("Page 3 of 3")).toBeInTheDocument()
  })

  it("navigates pages and disables controls at boundaries", async () => {
    renderViewer()
    await resolveViewerDocument()

    const previous = await screen.findByRole("button", { name: "Previous page" })
    const next = screen.getByRole("button", { name: "Next page" })
    expect(previous).toBeDisabled()

    fireEvent.click(next)
    expect(await screen.findByText("Page 2 of 3")).toBeInTheDocument()
    fireEvent.click(next)
    expect(await screen.findByText("Page 3 of 3")).toBeInTheDocument()
    expect(next).toBeDisabled()
  })

  it("renders thumbnails and navigates by selected thumbnail", async () => {
    renderViewer()
    await resolveViewerDocument()

    const firstPage = await screen.findByRole("button", { name: "Go to page 1" })
    expect(firstPage).toHaveAttribute("aria-current", "page")

    fireEvent.click(screen.getByRole("button", { name: "Go to page 3" }))

    expect(await screen.findByText("Page 3 of 3")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Go to page 3" })).toHaveAttribute("aria-current", "page")
    expect(await screen.findByLabelText("Reader Paper PDF thumbnail page 3")).toBeInTheDocument()
  })

  it("lazily renders thumbnails when they enter the observer margin", async () => {
    const intersection = installIntersectionObserverMock()
    renderViewer()
    await resolveViewerDocument()

    await waitFor(() => expect(intersection.getInstances()).toHaveLength(3))
    expect(intersection.getInstances()[0]?.options?.rootMargin).toBe("240px")
    expect(pdfjsMock.thumbnailRenderStarts).toEqual([])

    const firstPage = screen.getByRole("button", { name: "Go to page 1" })
    act(() => {
      intersection.trigger(firstPage, true)
    })

    await waitFor(() => expect(pdfjsMock.thumbnailRenderStarts).toContain(1))
    expect(await screen.findByLabelText("Reader Paper PDF thumbnail page 1")).toBeInTheDocument()
    intersection.restore()
  })

  it("limits concurrent thumbnail renders to two jobs", async () => {
    const intersection = installIntersectionObserverMock()
    pdfjsMock.holdThumbnailRenders()
    renderViewer()
    await resolveViewerDocument()
    await waitFor(() => expect(intersection.getInstances()).toHaveLength(3))

    for (const button of screen.getAllByRole("button", { name: /Go to page/ })) {
      act(() => {
        intersection.trigger(button, true)
      })
    }

    await waitFor(() => expect(pdfjsMock.thumbnailRenderStarts).toEqual([1, 2]))
    expect(pdfjsMock.getMaxActiveThumbnailRenderCount()).toBe(2)

    act(() => {
      pdfjsMock.releaseNextThumbnailRender()
    })

    await waitFor(() => expect(pdfjsMock.thumbnailRenderStarts).toEqual([1, 2, 3]))
    expect(pdfjsMock.getMaxActiveThumbnailRenderCount()).toBe(2)
    intersection.restore()
  })

  it("cancels active and pending thumbnail renders when thumbnails leave view", async () => {
    const intersection = installIntersectionObserverMock()
    pdfjsMock.holdThumbnailRenders()
    renderViewer()
    await resolveViewerDocument()
    await waitFor(() => expect(intersection.getInstances()).toHaveLength(3))

    const buttons = screen.getAllByRole("button", { name: /Go to page/ })
    for (const button of buttons) {
      act(() => {
        intersection.trigger(button, true)
      })
    }
    await waitFor(() => expect(pdfjsMock.thumbnailRenderStarts).toEqual([1, 2]))

    act(() => {
      intersection.trigger(buttons[0]!, false)
      intersection.trigger(buttons[2]!, false)
      pdfjsMock.releaseNextThumbnailRender()
      pdfjsMock.releaseNextThumbnailRender()
    })

    await waitFor(() => expect(pdfjsMock.thumbnailRenderCancels).toContain(1))
    expect(pdfjsMock.thumbnailRenderStarts).not.toContain(3)
    intersection.restore()
  })

  it("keeps the main viewer usable when a thumbnail render fails", async () => {
    pdfjsMock.pdf.getPage.mockImplementation(async (pageNumber: number) => {
      if (pageNumber === 2) {
        throw new Error("thumbnail failed")
      }
      return pdfjsMock.createPage(pageNumber)
    })

    renderViewer()
    await resolveViewerDocument()

    expect(await screen.findByText("Page 1 of 3")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Go to page 2" })).toHaveTextContent("Page 2")
  })

  it("supports keyboard page and zoom controls without crossing boundaries", async () => {
    renderViewer()
    await resolveViewerDocument()

    const viewer = await screen.findByLabelText("Reader Paper PDF viewer")
    fireEvent.keyDown(viewer, { key: "ArrowRight" })
    expect(await screen.findByText("Page 2 of 3")).toBeInTheDocument()

    fireEvent.keyDown(viewer, { key: "PageDown" })
    expect(await screen.findByText("Page 3 of 3")).toBeInTheDocument()

    fireEvent.keyDown(viewer, { key: "ArrowRight" })
    expect(screen.getByText("Page 3 of 3")).toBeInTheDocument()

    fireEvent.keyDown(viewer, { key: "+" })
    expect(await screen.findByText("125%")).toBeInTheDocument()

    fireEvent.keyDown(viewer, { key: "-" })
    fireEvent.keyDown(viewer, { key: "-" })
    expect(await screen.findByText("75%")).toBeInTheDocument()
  })

  it("clamps jump page input", async () => {
    renderViewer()
    await resolveViewerDocument()

    const input = await screen.findByLabelText("Page number")
    fireEvent.change(input, { target: { value: "99" } })
    fireEvent.submit(input.closest("form")!)
    expect(await screen.findByText("Page 3 of 3")).toBeInTheDocument()

    fireEvent.change(input, { target: { value: "-20" } })
    fireEvent.submit(input.closest("form")!)
    expect(await screen.findByText("Page 1 of 3")).toBeInTheDocument()
  })

  it("clamps zoom controls", async () => {
    renderViewer()
    await resolveViewerDocument()

    const zoomIn = await screen.findByRole("button", { name: "Zoom in" })
    const zoomOut = screen.getByRole("button", { name: "Zoom out" })
    fireEvent.click(zoomIn)
    fireEvent.click(zoomIn)
    fireEvent.click(zoomIn)
    fireEvent.click(zoomIn)
    fireEvent.click(zoomIn)
    expect(await screen.findByText("200%")).toBeInTheDocument()
    expect(zoomIn).toBeDisabled()

    fireEvent.click(zoomOut)
    fireEvent.click(zoomOut)
    fireEvent.click(zoomOut)
    fireEvent.click(zoomOut)
    fireEvent.click(zoomOut)
    fireEvent.click(zoomOut)
    expect(await screen.findByText("75%")).toBeInTheDocument()
    expect(zoomOut).toBeDisabled()
  })

  it("renders fallback and reports the real PDF loading failure", async () => {
    const onLoadError = vi.fn()
    renderViewer({ onLoadError, className: "portal-custom" })

    await waitFor(() => expect(pdfjsMock.getDocument).toHaveBeenCalled())
    await act(async () => {
      pdfjsMock.rejectDocument()
    })

    expect(await screen.findByText("PDF could not be rendered; showing text fallback.")).toBeInTheDocument()
    expect(screen.getByText("Text fallback content")).toBeInTheDocument()
    const banner = screen.getByText("PDF could not be rendered; showing text fallback.").closest("[data-pdf-failure]")
    expect(banner).toHaveClass("bg-warning/15", "text-warning-text")
    expect(banner?.parentElement).toHaveAttribute("data-pdf-appearance", "default")
    expect(banner?.parentElement).toHaveClass("min-h-[42rem]")
    expect(banner?.parentElement).not.toHaveClass("portal-custom")
    expect(onLoadError).toHaveBeenCalledOnce()
  })

  it("searches extracted text across pages with case-insensitive matches", async () => {
    renderViewer()
    await resolveViewerDocument()

    await waitFor(() => expect(pdfjsMock.getTextContent).toHaveBeenCalledWith(3))
    fireEvent.change(screen.getByLabelText("Search PDF text"), { target: { value: "LATENT" } })

    expect(await screen.findByText("2 results")).toBeInTheDocument()
    expect(screen.getByText(/Latent Transformer benchmark improves retrieval/i)).toBeInTheDocument()
    expect(screen.getByText(/Engineering notes mention LATENT planning/i)).toBeInTheDocument()
  })

  it("hides search results for short queries", async () => {
    renderViewer()
    await resolveViewerDocument()

    await waitFor(() => expect(pdfjsMock.getTextContent).toHaveBeenCalledWith(3))
    fireEvent.change(screen.getByLabelText("Search PDF text"), { target: { value: "l" } })

    expect(await screen.findByText("Enter at least 2 characters.")).toBeInTheDocument()
    expect(screen.queryByText(/Latent Transformer benchmark improves retrieval/i)).not.toBeInTheDocument()
  })

  it("navigates to clicked search results and reports page progress", async () => {
    const onPageChange = vi.fn()
    renderViewer({ onPageChange })
    await resolveViewerDocument()

    await waitFor(() => expect(pdfjsMock.getTextContent).toHaveBeenCalledWith(3))
    fireEvent.change(screen.getByLabelText("Search PDF text"), { target: { value: "latent" } })

    const thirdPageResult = await screen.findByText(/Engineering notes mention LATENT planning/i)
    fireEvent.click(thirdPageResult.closest("button")!)

    expect(await screen.findByText("Page 3 of 3")).toBeInTheDocument()
    await waitFor(() => expect(onPageChange).toHaveBeenCalledWith(3, 3))
  })

  it("clamps previous and next search result navigation", async () => {
    renderViewer()
    await resolveViewerDocument()

    await waitFor(() => expect(pdfjsMock.getTextContent).toHaveBeenCalledWith(3))
    fireEvent.change(screen.getByLabelText("Search PDF text"), { target: { value: "latent" } })

    expect(await screen.findByText("2 results")).toBeInTheDocument()
    const previous = screen.getByRole("button", { name: "Previous search result" })
    const next = screen.getByRole("button", { name: "Next search result" })
    expect(previous).toBeDisabled()

    fireEvent.click(next)
    expect(await screen.findByText("Page 3 of 3")).toBeInTheDocument()
    expect(next).toBeDisabled()

    fireEvent.click(previous)
    expect(await screen.findByText("Page 2 of 3")).toBeInTheDocument()
    expect(previous).toBeDisabled()
  })

  it("keeps the PDF canvas readable when text extraction fails", async () => {
    pdfjsMock.failTextContent()
    renderViewer()
    await resolveViewerDocument()

    expect(await screen.findByText("PDF search text is unavailable.")).toBeInTheDocument()
    expect(screen.getByLabelText("Reader Paper PDF page 1")).toBeInTheDocument()
    expect(screen.queryByText("Text fallback content")).not.toBeInTheDocument()
  })

  it("renders selectable text layer and keeps canvas usable when the text layer fails", async () => {
    const { rerender } = renderViewer()
    await resolveViewerDocument()

    expect(await screen.findByLabelText("Reader Paper selectable PDF text page 1")).toBeInTheDocument()
    await waitFor(() => expect(pdfjsMock.textLayerRender).toHaveBeenCalled())
    expect(screen.getByText("Diffusion")).toBeInTheDocument()

    pdfjsMock.reset()
    window.localStorage.clear()
    pdfjsMock.failTextLayer()
    rerender(
      <PaperPdfViewer
        pdfUrl="https://arxiv.org/pdf/2605.00001.pdf?text-layer-fail=1"
        title="Reader Paper"
        locale="en"
        fallback={<div>Text fallback content</div>}
      />
    )
    await waitFor(() => expect(pdfjsMock.getDocument).toHaveBeenCalled())
    await act(async () => {
      pdfjsMock.resolveDocument()
    })

    expect(await screen.findByText("Text selection unavailable")).toBeInTheDocument()
    expect(screen.getByLabelText("Reader Paper PDF page 1")).toBeInTheDocument()
    expect(screen.queryByText("Text fallback content")).not.toBeInTheDocument()
  })

  it("marks search hits and the active search result on the text layer", async () => {
    renderViewer()
    await resolveViewerDocument()

    await waitFor(() => expect(pdfjsMock.getTextContent).toHaveBeenCalledWith(3))
    fireEvent.change(screen.getByLabelText("Search PDF text"), { target: { value: "diffusion" } })

    const hit = await screen.findByText("Diffusion")
    await waitFor(() => expect(hit).toHaveClass("paper-pdf-search-hit"))
    expect(hit).toHaveClass("paper-pdf-search-hit-active")
  })

  it("uses valid local search cache and safely rebuilds corrupt cache", async () => {
    const cacheKey = "newsroom:paper-pdf-search:v1:reader-paper"
    window.localStorage.setItem(
      cacheKey,
      JSON.stringify({
        createdAt: Date.now(),
        numPages: 3,
        pageTexts: [
          { pageNumber: 1, text: "Cached context appears on page one." },
          { pageNumber: 2, text: "Cached latent result appears on page two." },
          { pageNumber: 3, text: "Cached benchmark detail appears on page three." }
        ]
      })
    )
    const firstRender = renderViewer({ paperId: "reader-paper" })
    await resolveViewerDocument()

    fireEvent.change(screen.getByLabelText("Search PDF text"), { target: { value: "cached" } })
    expect(await screen.findByText("3 results")).toBeInTheDocument()
    expect(pdfjsMock.getTextContent).not.toHaveBeenCalledWith(3)

    firstRender.unmount()
    pdfjsMock.reset()
    window.localStorage.setItem(cacheKey, "{not json")
    renderViewer({ paperId: "reader-paper", pdfUrl: "https://arxiv.org/pdf/2605.00001v2.pdf" })
    await resolveViewerDocument()

    await waitFor(() => expect(pdfjsMock.getTextContent).toHaveBeenCalledWith(3))
  })

  it("creates highlights and notes from selected text", async () => {
    const onCreateReaderNote = vi.fn()
    const restoreSelection = installSelectionMock()
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      bottom: 800,
      height: 800,
      left: 0,
      right: 600,
      top: 0,
      width: 600,
      x: 0,
      y: 0,
      toJSON: () => ({})
    } as DOMRect)

    renderViewer({ onCreateReaderNote })
    await resolveViewerDocument()

    fireEvent.mouseUp(await screen.findByLabelText("Reader Paper selectable PDF text page 1"))
    expect(await screen.findByText("Selected text")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Highlight" }))
    expect(onCreateReaderNote).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "highlight",
        pageNumber: 1,
        quote: "Diffusion model",
        anchor: expect.objectContaining({
          rects: [expect.objectContaining({ left: 10, top: 5, width: expect.any(Number), height: expect.any(Number) })]
        })
      })
    )

    fireEvent.mouseUp(screen.getByLabelText("Reader Paper selectable PDF text page 1"))
    fireEvent.change(await screen.findByLabelText("Note text"), { target: { value: "Important quote" } })
    fireEvent.click(screen.getByRole("button", { name: "Add note" }))
    expect(onCreateReaderNote).toHaveBeenLastCalledWith(
      expect.objectContaining({
        kind: "note",
        noteText: "Important quote",
        quote: "Diffusion model"
      })
    )
    restoreSelection()
  })

  it.each(["default", "paper-agent"] as const)("renders saved highlights over the current %s page after scale changes", async (appearance) => {
    renderViewer({
      appearance,
      notes: [
        {
          noteId: "note-1",
          userId: "user-1",
          paperId: "reader-paper",
          kind: "highlight",
          pageNumber: 1,
          color: "green",
          quote: "Diffusion model",
          anchor: {
            pageNumber: 1,
            quote: "Diffusion model",
            rects: [{ left: 12, top: 14, width: 28, height: 3 }]
          },
          createdAt: "2026-05-24T00:00:00Z",
          updatedAt: "2026-05-24T00:00:00Z"
        }
      ]
    })
    await resolveViewerDocument()

    const highlight = await screen.findByTitle("Diffusion model")
    expect(highlight).toHaveStyle({ left: "12%", top: "14%", width: "28%", height: "3%" })
    expect(highlight).toHaveClass("bg-success/30")
    expect(highlight.parentElement?.parentElement).toHaveAttribute("data-pdf-document")
    expect(highlight.parentElement?.parentElement?.parentElement).toHaveAttribute("data-pdf-page-flow")

    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }))
    expect(await screen.findByText("125%")).toBeInTheDocument()
    expect(screen.getByTitle("Diffusion model")).toBeInTheDocument()
  })
})
