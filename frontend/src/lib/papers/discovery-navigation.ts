import { papersRoutes } from "./routes"

const returnPaths = new Set(["/design-demo", "/design-demo/papers", "/papers", "/papers/methods", "/papers/tasks"])

export function safePaperReturnTo(value?: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || /[\\\r\n]/.test(value)) return "/papers"
  try {
    const url = new URL(value, "https://agora.invalid")
    if (url.origin !== "https://agora.invalid" || !returnPaths.has(url.pathname)) return "/papers"
    url.searchParams.delete("paper")
    return `${url.pathname}${url.search}`
  } catch {
    return "/papers"
  }
}

export function paperReaderHref(slug: string, returnTo?: string) {
  const href = safePaperReturnTo(returnTo).startsWith("/design-demo/papers")
    ? `/design-demo${papersRoutes.reader(slug)}`
    : papersRoutes.reader(slug)
  return returnTo ? `${href}?${new URLSearchParams({ returnTo: safePaperReturnTo(returnTo) })}` : href
}

export function rememberPaperScroll(returnTo: string) {
  try {
    sessionStorage.setItem(`paper-scroll:${safePaperReturnTo(returnTo)}`, String(window.scrollY))
  } catch {
    // Storage is optional; URL navigation still preserves the research state.
  }
}

export function restorePaperScroll(returnTo: string) {
  try {
    const key = `paper-scroll:${safePaperReturnTo(returnTo)}`
    const value = sessionStorage.getItem(key)
    if (value === null) return
    sessionStorage.removeItem(key)
    const top = Number(value)
    if (Number.isFinite(top) && top >= 0) window.scrollTo({ top, behavior: "instant" })
  } catch {
    // A blocked browser store must not interrupt paper discovery.
  }
}

export function hasSavedPaperScroll(returnTo: string) {
  try {
    return sessionStorage.getItem(`paper-scroll:${safePaperReturnTo(returnTo)}`) !== null
  } catch { return false }
}

export function paperQuestionHref(question: string) {
  return `/design-demo/papers?${new URLSearchParams({ question: question.trim(), q: question.trim() })}`
}
