"use client"

import { useEffect } from "react"
import { usePathname, useSearchParams } from "next/navigation"
import { readResearchHistory, recordResearchVisit, prepareResearchResume, takeResearchResume } from "@/lib/research/history"
import { researchModuleForPath } from "@/lib/research/entry"
import { useResearchHistory } from "@/lib/research/use-research-history"
import { historyState } from "@/lib/research/history"

export function ResearchHistoryTracker({ enabled = true, expectedOwner }: { enabled?: boolean; expectedOwner?: string | null }) {
  const history = useResearchHistory()
  const ready = enabled && history.ready && (expectedOwner === undefined || expectedOwner === history.owner)
  const pathname = usePathname()
  const search = useSearchParams().toString()

  useEffect(() => {
    if (!ready) return
    const onBack = () => {
      const href = window.location.pathname + window.location.search
      const visit = readResearchHistory().find(item => item.href === href)
      if (visit) prepareResearchResume(visit)
    }
    window.addEventListener("popstate", onBack)
    return () => window.removeEventListener("popstate", onBack)
  }, [ready])

  useEffect(() => {
    if (!ready || !researchModuleForPath(pathname) || !new URLSearchParams(search).get("question")) return
    const href = pathname + (search ? `?${search}` : "")
    const resume = takeResearchResume(href)
    let restoring = Boolean(resume && resume.scrollY > 0)
    let lastScroll = resume?.scrollY ?? window.scrollY
    let timer: ReturnType<typeof setTimeout> | undefined
    let observer: ResizeObserver | undefined
    const owner = historyState().owner
    const save = () => { if (historyState().owner === owner) recordResearchVisit(href, lastScroll) }
    const tryRestore = () => {
      if (!restoring || !resume) return
      if (document.documentElement.scrollHeight - window.innerHeight >= resume.scrollY) {
        window.scrollTo({ top: resume.scrollY, behavior: "instant" })
        restoring = false
        observer?.disconnect()
        save()
      }
    }
    const finishRestore = () => {
      restoring = false
      observer?.disconnect()
      lastScroll = window.scrollY
      save()
    }
    const onScroll = () => {
      if (restoring) return
      lastScroll = window.scrollY
      if (timer) clearTimeout(timer)
      timer = setTimeout(save, 150)
    }
    save()
    if (restoring) {
      observer = new ResizeObserver(tryRestore)
      observer.observe(document.body)
      tryRestore()
    }
    // A shorter result set can no longer reach the old position; stop waiting after loading.
    const deadline = setTimeout(finishRestore, 8000)
    window.addEventListener("scroll", onScroll, { passive: true })
    window.addEventListener("wheel", finishRestore, { passive: true, once: true })
    window.addEventListener("pagehide", save)
    return () => {
      if (timer) clearTimeout(timer)
      clearTimeout(deadline)
      observer?.disconnect()
      if (restoring && resume && historyState().owner === owner) prepareResearchResume(resume)
      window.removeEventListener("scroll", onScroll)
      window.removeEventListener("wheel", finishRestore)
      window.removeEventListener("pagehide", save)
      save()
    }
  }, [pathname, search, ready])
  return null
}
