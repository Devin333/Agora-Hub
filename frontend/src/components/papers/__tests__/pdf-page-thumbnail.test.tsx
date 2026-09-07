import { act, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { PdfPageThumbnail } from "@/components/papers/pdf-page-thumbnail"

const getDocument = vi.hoisted(() => vi.fn())
vi.mock("pdfjs-dist", () => ({ getDocument, GlobalWorkerOptions: { workerSrc: "" } }))

function mockDocument() {
  const renderTask = { cancel: vi.fn(), promise: Promise.resolve() }
  const page = { getViewport: vi.fn(() => ({ width: 600, height: 800 })), render: vi.fn(() => renderTask) }
  const pdf = { getPage: vi.fn().mockResolvedValue(page) }
  const loadingTask = { promise: Promise.resolve(pdf), destroy: vi.fn().mockResolvedValue(undefined) }
  getDocument.mockReturnValue(loadingTask)
  return { loadingTask, renderTask, pdf }
}

describe("PdfPageThumbnail resource lifecycle", () => {
  beforeEach(() => {
    getDocument.mockReset()
    vi.stubGlobal("IntersectionObserver", undefined)
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({} as CanvasRenderingContext2D)
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it("destroys a completed loading task once, including later unmount", async () => {
    const { loadingTask } = mockDocument()
    const { unmount } = render(<PdfPageThumbnail locale="en" title="Paper" pdfUrl="https://arxiv.org/pdf/test" />)
    await waitFor(() => expect(loadingTask.destroy).toHaveBeenCalledTimes(1))
    expect(screen.getByLabelText("Paper PDF first page")).toBeVisible()
    unmount()
    await act(async () => {})
    expect(loadingTask.destroy).toHaveBeenCalledTimes(1)
  })

  it("releases failed document loads before displaying the unavailable state", async () => {
    const { loadingTask, pdf } = mockDocument()
    pdf.getPage.mockRejectedValue(new Error("invalid first page"))
    render(<PdfPageThumbnail locale="en" title="Paper" pdfUrl="https://arxiv.org/pdf/test" />)
    expect(await screen.findByText("PDF first page unavailable")).toBeInTheDocument()
    await waitFor(() => expect(loadingTask.destroy).toHaveBeenCalledTimes(1))
  })

  it("awaits one cancellation when unmounted during a pending load", async () => {
    let rejectLoad: (error: Error) => void = () => undefined
    const promise = new Promise((_, reject) => { rejectLoad = reject })
    const destroy = vi.fn().mockImplementation(async () => { rejectLoad(new Error("Worker was terminated")) })
    getDocument.mockReturnValue({ promise, destroy })
    const { unmount } = render(<PdfPageThumbnail locale="en" title="Paper" pdfUrl="https://arxiv.org/pdf/test" />)
    await waitFor(() => expect(getDocument).toHaveBeenCalledTimes(1))
    unmount()
    await act(async () => {})
    expect(destroy).toHaveBeenCalledTimes(1)
  })
})
