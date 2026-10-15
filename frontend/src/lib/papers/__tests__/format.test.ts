import { describe, expect, it } from "vitest"
import { formatPaperDate, normalizePdfUrl, paperPdfUrlFromSource } from "@/lib/papers/format"

describe("paper PDF URL helpers", () => {
  it("derives arXiv PDF URLs", () => {
    expect(paperPdfUrlFromSource("https://arxiv.org/abs/2508.03680")).toBe("https://arxiv.org/pdf/2508.03680.pdf")
    expect(paperPdfUrlFromSource("http://arxiv.org/pdf/2508.03680")).toBe("https://arxiv.org/pdf/2508.03680.pdf")
  })

  it("derives PDF URLs for common paper sites", () => {
    expect(paperPdfUrlFromSource("https://openreview.net/forum?id=abc123")).toBe("https://openreview.net/pdf?id=abc123")
    expect(paperPdfUrlFromSource("https://aclanthology.org/2024.acl-long.1/")).toBe("https://aclanthology.org/2024.acl-long.1.pdf")
    expect(paperPdfUrlFromSource("https://proceedings.mlr.press/v202/lee23g.html")).toBe("https://proceedings.mlr.press/v202/lee23g/lee23g.pdf")
    expect(paperPdfUrlFromSource("https://openaccess.thecvf.com/content/CVPR2024/html/Li_Example_Paper_CVPR_2024_paper.html")).toBe(
      "https://openaccess.thecvf.com/content/CVPR2024/papers/Li_Example_Paper_CVPR_2024_paper.pdf"
    )
  })

  it("only accepts explicit PDF URLs when a source cannot be safely inferred", () => {
    expect(normalizePdfUrl("https://papers.nips.cc/paper_files/paper/2024/file/example-Paper-Conference.pdf")).toBe(
      "https://papers.nips.cc/paper_files/paper/2024/file/example-Paper-Conference.pdf"
    )
    expect(normalizePdfUrl("https://example.com/paper.html")).toBeUndefined()
    expect(paperPdfUrlFromSource("https://example.com/paper.html")).toBeUndefined()
  })
})


describe("formatPaperDate", () => {
  it("formats known paper dates for each supported locale", () => {
    expect(formatPaperDate("2024-05-06", "zh")).toBe("2024年5月6日")
    expect(formatPaperDate("2024-05-06T12:30:00Z", "en")).toBe("May 6, 2024")
    expect(formatPaperDate("2024-02-29", "en")).toBe("Feb 29, 2024")
  })

  it.each(["", "   ", null, undefined])("returns an explicit unknown date for missing runtime value %s", value => {
    expect(formatPaperDate(value, "zh")).toBe("日期未知")
    expect(formatPaperDate(value, "en")).toBe("Unknown date")
  })

  it.each([
    "not-a-date",
    "2024-13-01",
    "2024-00-10",
    "2024-04-31",
    "2023-02-29",
    "2024-02-30",
    "2024-05-06T25:00:00Z",
    "999999-12-31",
  ])("returns an explicit unknown date for invalid or overflowing value %s", value => {
    expect(formatPaperDate(value, "zh")).toBe("日期未知")
    expect(formatPaperDate(value, "en")).toBe("Unknown date")
  })
})
