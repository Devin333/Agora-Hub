import type { ReactElement } from "react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import PapersMethodDetailPageRoute from "@/app/papers/methods/[slug]/page"
import PapersTaskDetailPageRoute from "@/app/papers/tasks/[slug]/page"
import { notFound } from "next/navigation"
import { getPaperMethodsResult, getPaperTasksResult, getPublishedPapers } from "@/lib/papers/real-data"
import type { Paper, PaperMethod, PaperTask } from "@/lib/papers/types"

vi.mock("next/navigation", () => ({
  notFound: vi.fn(() => {
    throw new Error("NEXT_NOT_FOUND")
  })
}))

vi.mock("@/lib/papers/real-data", () => ({
  getPaperMethodsResult: vi.fn(),
  getPaperTasksResult: vi.fn(),
  getPublishedPapers: vi.fn()
}))

const task: PaperTask = {
  id: "task-backend",
  slug: "backend-task",
  name: "Backend Task",
  group: "language-models",
  description: "Backend task.",
  paperCount: 1,
  benchmarkCount: 0,
  methodCount: 1,
  sisterTasks: [],
  commonMethods: []
}

const method: PaperMethod = {
  id: "method-backend",
  slug: "backend-method",
  name: "Backend Method",
  description: "Backend method.",
  paperCount: 1,
  taskCount: 1,
  implementationCount: 0,
  area: "Agents",
  relatedTasks: [],
  relatedMethods: []
}

const papers: Paper[] = [
  {
    id: "paper-1",
    slug: "paper-1",
    title: "Paper One",
    abstractSnippet: "A paper.",
    authors: ["NewsRoom"],
    publishedAt: "2026-05-24T00:00:00Z",
    tags: [],
    taskRefs: [{ id: task.id, slug: task.slug, name: task.name }],
    methodRefs: [{ id: method.id, slug: method.slug, name: method.name }],
    isPublished: true
  }
]

describe("paper taxonomy detail routes", () => {
  beforeEach(() => {
    vi.mocked(getPaperMethodsResult).mockReset()
    vi.mocked(getPaperTasksResult).mockReset()
    vi.mocked(getPublishedPapers).mockReset()
  })

  it("passes backend task notices through to the task detail client", async () => {
    vi.mocked(getPaperTasksResult).mockResolvedValueOnce({
      items: [task],
      source: "backend",
      dataState: "ready",
      notices: ["Backend paper API is unavailable; showing tracked paper cache."]
    })
    vi.mocked(getPublishedPapers).mockResolvedValueOnce(papers)

    const element = await PapersTaskDetailPageRoute({ params: Promise.resolve({ slug: "backend-task" }) }) as ReactElement<{
      fallbackNotice?: string | null
      papers: Paper[]
      task: PaperTask
    }>

    expect(element.props.task).toBe(task)
    expect(element.props.papers).toBe(papers)
    expect(element.props.fallbackNotice).toBe("Backend paper API is unavailable; showing tracked paper cache.")
  })

  it("passes backend method notices through to the method detail client", async () => {
    vi.mocked(getPaperMethodsResult).mockResolvedValueOnce({
      items: [method],
      source: "backend",
      dataState: "ready",
      notices: ["Backend paper API is unavailable; showing tracked paper cache."]
    })
    vi.mocked(getPublishedPapers).mockResolvedValueOnce(papers)

    const element = await PapersMethodDetailPageRoute({ params: Promise.resolve({ slug: "backend-method" }) }) as ReactElement<{
      fallbackNotice?: string | null
      method: PaperMethod
      papers: Paper[]
    }>

    expect(element.props.method).toBe(method)
    expect(element.props.papers).toBe(papers)
    expect(element.props.fallbackNotice).toBe("Backend paper API is unavailable; showing tracked paper cache.")
  })

  it("does not show static task detail pages when public taxonomy data is empty", async () => {
    vi.mocked(getPaperTasksResult).mockResolvedValueOnce({
      items: [],
      source: "taxonomy",
      dataState: "empty",
      notices: ["No public papers are available."]
    })

    await expect(PapersTaskDetailPageRoute({ params: Promise.resolve({ slug: "coding-agents" }) })).rejects.toThrow("NEXT_NOT_FOUND")
    expect(notFound).toHaveBeenCalled()
    expect(getPublishedPapers).not.toHaveBeenCalled()
  })

  it("does not show task detail pages for zero-paper taxonomy items", async () => {
    vi.mocked(getPaperTasksResult).mockResolvedValueOnce({
      items: [{ ...task, paperCount: 0, benchmarkCount: 0, methodCount: 0 }],
      source: "backend",
      dataState: "ready",
      notices: []
    })

    await expect(PapersTaskDetailPageRoute({ params: Promise.resolve({ slug: "backend-task" }) })).rejects.toThrow("NEXT_NOT_FOUND")
    expect(notFound).toHaveBeenCalled()
    expect(getPublishedPapers).not.toHaveBeenCalled()
  })

  it("does not show static method detail pages when backend taxonomy excludes the slug", async () => {
    vi.mocked(getPaperMethodsResult).mockResolvedValueOnce({
      items: [method],
      source: "backend",
      dataState: "ready",
      notices: []
    })

    await expect(PapersMethodDetailPageRoute({ params: Promise.resolve({ slug: "large-language-model" }) })).rejects.toThrow("NEXT_NOT_FOUND")
    expect(notFound).toHaveBeenCalled()
    expect(getPublishedPapers).not.toHaveBeenCalled()
  })

  it("does not show method detail pages for zero-paper taxonomy items", async () => {
    vi.mocked(getPaperMethodsResult).mockResolvedValueOnce({
      items: [{ ...method, paperCount: 0, taskCount: 0, implementationCount: 0 }],
      source: "backend",
      dataState: "ready",
      notices: []
    })

    await expect(PapersMethodDetailPageRoute({ params: Promise.resolve({ slug: "backend-method" }) })).rejects.toThrow("NEXT_NOT_FOUND")
    expect(notFound).toHaveBeenCalled()
    expect(getPublishedPapers).not.toHaveBeenCalled()
  })
})
