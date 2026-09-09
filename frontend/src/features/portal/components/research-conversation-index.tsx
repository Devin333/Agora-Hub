"use client"

import * as TooltipPrimitive from "@radix-ui/react-tooltip"
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type RefObject } from "react"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import styles from "./research-conversation-index.module.css"

export type ConversationIndexEntry = { id: string; label: string }

type ResearchConversationIndexProps = {
  entries: ConversationIndexEntry[]
  transcriptRef: RefObject<HTMLDivElement>
}

const PREVIEW_LENGTH = 160

function previewLabel(label: string) {
  const normalized = label.replace(/\s+/g, " ").trim()
  if (!normalized) return "空消息"
  const characters = Array.from(normalized)
  return characters.length <= PREVIEW_LENGTH ? normalized : `${characters.slice(0, PREVIEW_LENGTH - 1).join("")}…`
}

function findEntryTarget(transcript: HTMLDivElement, id: string) {
  const target = document.getElementById(id)
  return target && transcript.contains(target) ? (target as HTMLElement) : null
}

function scheduleFrame(callback: (time: number) => void) {
  if (typeof window.requestAnimationFrame === "function") return window.requestAnimationFrame(callback)
  return window.setTimeout(() => callback(performance.now()), 0)
}

function cancelFrame(frame: number) {
  if (typeof window.cancelAnimationFrame === "function") window.cancelAnimationFrame(frame)
  else window.clearTimeout(frame)
}

export function ResearchConversationIndex({ entries, transcriptRef }: ResearchConversationIndexProps) {
  const [activeIndex, setActiveIndex] = useState(0)
  const navRef = useRef<HTMLElement>(null)

  const updateActiveIndex = useCallback(() => {
    const transcript = transcriptRef.current
    if (!transcript || entries.length === 0) {
      setActiveIndex(0)
      return
    }

    const transcriptRect = transcript.getBoundingClientRect()
    const readingTop = transcript.scrollTop + 24
    const readingBottom = transcriptRect.top + transcript.clientHeight
    let current = -1
    let firstVisible = -1
    let lastVisible = -1

    entries.forEach((entry, index) => {
      const target = findEntryTarget(transcript, entry.id)
      if (!target) return
      const targetRect = target.getBoundingClientRect()
      const documentTop = transcript.scrollTop + targetRect.top - transcriptRect.top
      // Scroll offsets are rounded to device pixels; the target may land a fraction below the inset.
      if (documentTop <= readingTop + 1) current = index
      if (targetRect.bottom > transcriptRect.top && targetRect.top < readingBottom) {
        if (firstVisible < 0) firstVisible = index
        lastVisible = index
      }
    })

    const atBottom = transcript.scrollTop + transcript.clientHeight >= transcript.scrollHeight - 1
    if (atBottom && lastVisible >= 0) current = lastVisible
    if (current < 0) current = firstVisible >= 0 ? firstVisible : 0
    setActiveIndex(previous => previous === current ? previous : current)
  }, [entries, transcriptRef])

  useEffect(() => {
    const transcript = transcriptRef.current
    if (!transcript) {
      setActiveIndex(0)
      return
    }

    let frame: number | null = null
    const scheduleUpdate = () => {
      if (frame !== null) return
      frame = scheduleFrame(() => {
        frame = null
        updateActiveIndex()
      })
    }

    transcript.addEventListener("scroll", scheduleUpdate, { passive: true })
    window.addEventListener("resize", scheduleUpdate)

    const resizeObserver = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(scheduleUpdate)
    resizeObserver?.observe(transcript)
    Array.from(transcript.children).forEach(child => resizeObserver?.observe(child))
    entries.forEach(entry => {
      const target = findEntryTarget(transcript, entry.id)
      if (target) resizeObserver?.observe(target)
    })

    updateActiveIndex()
    const initialFrame = scheduleFrame(() => {
      updateActiveIndex()
      scheduleUpdate()
    })

    return () => {
      transcript.removeEventListener("scroll", scheduleUpdate)
      window.removeEventListener("resize", scheduleUpdate)
      resizeObserver?.disconnect()
      if (frame !== null) cancelFrame(frame)
      cancelFrame(initialFrame)
    }
  }, [entries, transcriptRef, updateActiveIndex])

  useEffect(() => {
    const nav = navRef.current
    const button = nav?.querySelector<HTMLButtonElement>('[aria-current="location"]')
    if (!nav || !button || nav.contains(document.activeElement)) return
    const navBox = nav.getBoundingClientRect(), buttonBox = button.getBoundingClientRect()
    if (buttonBox.top < navBox.top) nav.scrollTop -= navBox.top - buttonBox.top + 4
    else if (buttonBox.bottom > navBox.bottom) nav.scrollTop += buttonBox.bottom - navBox.bottom + 4
  }, [activeIndex])

  const focusButton = useCallback((index: number) => {
    const buttons = navRef.current?.querySelectorAll<HTMLButtonElement>("button")
    buttons?.[index]?.focus()
  }, [])

  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | null = null
    if (event.key === "ArrowUp") nextIndex = Math.max(0, index - 1)
    if (event.key === "ArrowDown") nextIndex = Math.min(entries.length - 1, index + 1)
    if (event.key === "Home") nextIndex = 0
    if (event.key === "End") nextIndex = entries.length - 1
    if (nextIndex === null) return
    event.preventDefault()
    focusButton(nextIndex)
  }, [entries.length, focusButton])

  const scrollToEntry = useCallback((index: number) => {
    const transcript = transcriptRef.current
    const target = transcript && findEntryTarget(transcript, entries[index]?.id)
    if (!transcript || !target) return

    const transcriptRect = transcript.getBoundingClientRect()
    const targetRect = target.getBoundingClientRect()
    const requestedTop = transcript.scrollTop + targetRect.top - transcriptRect.top - 24
    const top = Math.max(0, requestedTop)
    const reducedMotion = typeof window.matchMedia !== "function" || window.matchMedia("(prefers-reduced-motion: reduce)").matches
    transcript.scrollTo({ top, behavior: reducedMotion ? "auto" : "smooth" })
    setActiveIndex(index)
  }, [entries, transcriptRef])

  const currentIndex = entries.length === 0 ? -1 : Math.min(activeIndex, entries.length - 1)
  if (entries.length === 0) return null

  return <TooltipProvider delayDuration={180}>
  <nav ref={navRef} className={styles.index} aria-label="聊天索引">
    <ol className={styles.list}>
      {entries.map((entry, index) => {
        const preview = previewLabel(entry.label)
        const current = index === currentIndex
        return <li key={entry.id} className={styles.item}>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                className={`${styles.button} ${current ? styles.current : ""}`}
                aria-label={`跳转到第 ${index + 1} 条消息：${preview}`}
                aria-current={current ? "location" : undefined}
                data-conversation-index={index}
                onClick={() => scrollToEntry(index)}
                onKeyDown={event => handleKeyDown(event, index)}
              >
                <span className={styles.mark} aria-hidden="true" />
              </button>
            </TooltipTrigger>
            <TooltipPrimitive.Portal>
              <TooltipContent className={styles.preview} side="right" sideOffset={8} collisionPadding={8}>
                第 {index + 1} 条：{preview}
              </TooltipContent>
            </TooltipPrimitive.Portal>
          </Tooltip>
        </li>
      })}
    </ol>
  </nav>
  </TooltipProvider>
}
