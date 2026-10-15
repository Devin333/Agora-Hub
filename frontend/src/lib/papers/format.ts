import type { Locale, MethodRef, Paper, PaperMethod, PaperSort, PaperTask, TaskRef } from "@/lib/papers/types"

export function paperTitle(paper: Paper, locale: Locale) {
  return locale === "zh" ? paper.titleZh ?? paper.title : paper.title
}

export function paperSnippet(paper: Paper, locale: Locale) {
  return locale === "zh" ? paper.abstractSnippetZh ?? paper.abstractSnippet : paper.abstractSnippet
}

export function taskName(task: PaperTask | TaskRef, locale: Locale) {
  return locale === "zh" ? task.nameZh ?? task.name : task.name
}

export function taskDescription(task: PaperTask, locale: Locale) {
  return locale === "zh" ? task.descriptionZh ?? task.description : task.description
}

export function methodName(method: PaperMethod | MethodRef, locale: Locale) {
  return locale === "zh" ? method.nameZh ?? method.name : method.name
}

export function methodDescription(method: PaperMethod, locale: Locale) {
  return locale === "zh" ? method.descriptionZh ?? method.description : method.description
}

export function formatPaperDate(value: string | null | undefined, locale: Locale) {
  const normalizedValue = value?.trim()
  if (!normalizedValue || !hasValidCalendarDate(normalizedValue)) {
    return unknownPaperDate(locale)
  }

  const date = new Date(normalizedValue)
  if (!Number.isFinite(date.getTime())) {
    return unknownPaperDate(locale)
  }

  return new Intl.DateTimeFormat(locale === "zh" ? "zh-CN" : "en-US", {
    year: "numeric",
    month: "short",
    day: "numeric"
  }).format(date)
}

function hasValidCalendarDate(value: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:$|T)/.exec(value)
  if (!match) {
    return true
  }

  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const daysInMonth = [31, isLeapYear(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

  return month >= 1 && month <= 12 && day >= 1 && day <= daysInMonth[month - 1]
}

function isLeapYear(year: number) {
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
}

function unknownPaperDate(locale: Locale) {
  return locale === "zh" ? "日期未知" : "Unknown date"
}

export function formatCompactNumber(value?: number) {
  if (!value) {
    return "0"
  }
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(value)
}

export function formatWholeNumber(value: number, locale: Locale) {
  return new Intl.NumberFormat(locale === "zh" ? "zh-CN" : "en-US").format(value)
}

export function paperPdfUrl(paper: Pick<Paper, "pdfUrl" | "arxivUrl" | "paperUrl">) {
  return normalizePdfUrl(paper.pdfUrl) ?? paperPdfUrlFromSource(paper.arxivUrl) ?? paperPdfUrlFromSource(paper.paperUrl)
}

export function paperPdfUrlFromSource(value?: string) {
  if (!value) {
    return undefined
  }

  const parsedUrl = parsePaperUrl(value)
  if (!parsedUrl) {
    return undefined
  }

  const host = parsedUrl.hostname.toLowerCase().replace(/^www\./, "")

  if (host === "arxiv.org") {
    return arxivPdfUrl(parsedUrl.toString())
  }

  if (host === "openreview.net") {
    return openReviewPdfUrl(parsedUrl)
  }

  if (host === "aclanthology.org") {
    return aclAnthologyPdfUrl(parsedUrl)
  }

  if (host === "proceedings.mlr.press") {
    return pmlrPdfUrl(parsedUrl)
  }

  if (host === "openaccess.thecvf.com") {
    return cvfPdfUrl(parsedUrl)
  }

  if (host === "papers.nips.cc" || host === "proceedings.neurips.cc") {
    return neuripsPdfUrl(parsedUrl)
  }

  return undefined
}

export function normalizePdfUrl(value?: string) {
  const parsedUrl = parsePaperUrl(value)
  if (!parsedUrl) {
    return undefined
  }

  const path = parsedUrl.pathname.toLowerCase()
  if (path.endsWith(".pdf") || isOpenReviewPdfRoute(parsedUrl) || isArxivPdfRoute(parsedUrl)) {
    return parsedUrl.toString()
  }

  return undefined
}

export function arxivPdfUrl(value?: string) {
  const parsedUrl = parsePaperUrl(value)
  if (!parsedUrl || parsedUrl.hostname.toLowerCase().replace(/^www\./, "") !== "arxiv.org") {
    return undefined
  }

  if (isArxivPdfRoute(parsedUrl)) {
    return ensurePdfExtension(parsedUrl.toString())
  }

  if (parsedUrl.pathname.startsWith("/abs/")) {
    const arxivId = parsedUrl.pathname.replace(/^\/abs\//, "").replace(/\/$/, "")
    return arxivId ? `https://arxiv.org/pdf/${arxivId}.pdf` : undefined
  }

  return undefined
}

function openReviewPdfUrl(url: URL) {
  if (isOpenReviewPdfRoute(url)) {
    return url.toString()
  }

  if (url.pathname === "/forum" && url.searchParams.get("id")) {
    const pdfUrl = new URL("https://openreview.net/pdf")
    pdfUrl.searchParams.set("id", url.searchParams.get("id") ?? "")
    return pdfUrl.toString()
  }

  return undefined
}

function aclAnthologyPdfUrl(url: URL) {
  if (url.pathname.toLowerCase().endsWith(".pdf")) {
    return url.toString()
  }

  const paperId = url.pathname.replace(/^\/+|\/+$/g, "")
  if (!paperId || paperId.includes("/")) {
    return undefined
  }

  return `https://aclanthology.org/${paperId}.pdf`
}

function pmlrPdfUrl(url: URL) {
  if (url.pathname.toLowerCase().endsWith(".pdf")) {
    return url.toString()
  }

  const match = url.pathname.match(/^\/(v\d+)\/([^/]+)\.html$/i)
  if (!match) {
    return undefined
  }

  const [, volume, paperId] = match
  return `https://proceedings.mlr.press/${volume}/${paperId}/${paperId}.pdf`
}

function cvfPdfUrl(url: URL) {
  if (url.pathname.toLowerCase().endsWith(".pdf")) {
    return url.toString()
  }

  if (!url.pathname.includes("/html/") || !url.pathname.toLowerCase().endsWith(".html")) {
    return undefined
  }

  return `https://openaccess.thecvf.com${url.pathname.replace("/html/", "/papers/").replace(/\.html$/i, ".pdf")}`
}

function neuripsPdfUrl(url: URL) {
  if (url.pathname.toLowerCase().endsWith(".pdf")) {
    return url.toString()
  }

  const match = url.pathname.match(/^(.*\/paper_files\/paper\/\d+\/)hash\/([^/]+)-Abstract(?:-([A-Za-z]+))?\.html$/)
  if (!match) {
    return undefined
  }

  const [, prefix, paperHash, suffix] = match
  const suffixPart = suffix ? `-${suffix}` : ""
  return `https://${url.hostname}${prefix}file/${paperHash}-Paper${suffixPart}.pdf`
}

function parsePaperUrl(value?: string) {
  if (!value) {
    return undefined
  }

  const cleanValue = value.trim().split("#")[0]
  if (!cleanValue) {
    return undefined
  }

  try {
    const parsedUrl = new URL(cleanValue.replace(/^http:\/\//i, "https://"))
    return parsedUrl.protocol === "https:" ? parsedUrl : undefined
  } catch {
    return undefined
  }
}

function isArxivPdfRoute(url: URL) {
  return url.hostname.toLowerCase().replace(/^www\./, "") === "arxiv.org" && url.pathname.startsWith("/pdf/")
}

function isOpenReviewPdfRoute(url: URL) {
  return url.hostname.toLowerCase().replace(/^www\./, "") === "openreview.net" && url.pathname === "/pdf" && Boolean(url.searchParams.get("id"))
}

function ensurePdfExtension(value: string) {
  const url = new URL(value)
  return url.pathname.toLowerCase().endsWith(".pdf") ? url.toString() : `${url.toString()}.pdf`
}

export function sortPapers(papers: Paper[], sort: PaperSort) {
  const sorted = [...papers]

  if (sort === "newest") {
    return sorted.sort((left, right) => new Date(right.publishedAt).getTime() - new Date(left.publishedAt).getTime())
  }

  if (sort === "most_cited") {
    return sorted.sort((left, right) => (right.citationCount ?? 0) - (left.citationCount ?? 0))
  }

  return sorted.sort((left, right) => {
    const rightMomentum = (right.githubStars ?? 0) + (right.githubMomentum ?? 0) * 100 + (right.citationCount ?? 0) * 2
    const leftMomentum = (left.githubStars ?? 0) + (left.githubMomentum ?? 0) * 100 + (left.citationCount ?? 0) * 2
    return rightMomentum - leftMomentum
  })
}
