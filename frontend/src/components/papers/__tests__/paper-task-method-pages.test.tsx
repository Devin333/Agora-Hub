import { fireEvent, render, screen, within } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"
import { MethodDetailPage } from "@/components/papers/methods/method-detail-page"
import { BenchmarkEvidencePanel } from "@/components/papers/shared/benchmark-evidence-panel"
import { TaskDetailPage } from "@/components/papers/tasks/task-detail-page"
import type { Paper, PaperMethod } from "@/lib/papers/types"

const methodDetail: PaperMethod = {
  id: "method-tool-use",
  slug: "tool-use",
  name: "Tool Use",
  description: "Uses external tools and environments.",
  paperCount: 2,
  taskCount: 1,
  implementationCount: 1,
  area: "Agents",
  relatedTasks: [],
  relatedMethods: [],
  commonBenchmarks: [
    {
      id: "benchmark-swe-bench",
      slug: "swe-bench",
      name: "SWE-bench",
      category: "software-engineering"
    }
  ]
}

const methodPapers: Paper[] = [
  {
    id: "paper-swe",
    slug: "paper-swe",
    title: "SWE-agent",
    abstractSnippet: "Agent-computer interfaces evaluated on software engineering tasks.",
    authors: ["A"],
    publishedAt: "2026-05-24T00:00:00Z",
    tags: ["agents"],
    taskRefs: [{ id: "task-coding", slug: "coding-agents", name: "Coding Agents" }],
    methodRefs: [{ id: "method-tool-use", slug: "tool-use", name: "Tool Use" }],
    benchmarks: [
      {
        id: "benchmark-swe-bench",
        name: "SWE-bench",
        category: "software-engineering",
        metric: "resolved",
        value: "12.5%"
      }
    ],
    paperUrl: "https://arxiv.org/abs/2605.00001",
    isPublished: true
  }
]

const taskDetail = {
  id: "task-coding",
  slug: "coding-agents",
  name: "Coding Agents",
  group: "code-ai",
  description: "Agents that solve software engineering tasks.",
  paperCount: 1,
  benchmarkCount: 1,
  methodCount: 1,
  sisterTasks: [],
  commonMethods: []
}

describe("paper task and method detail pages", () => {
  it("opens benchmark evidence from method detail instead of placeholder copy", () => {
    render(<MethodDetailPage method={methodDetail} locale="en" papers={methodPapers} />)

    expect(screen.getByLabelText(/papers: 1/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/tasks: 1/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/implementations: 0/i)).toBeInTheDocument()
    const relatedTasksPanel = screen.getByText("Related Tasks").closest("section")
    expect(relatedTasksPanel).not.toBeNull()
    expect(within(relatedTasksPanel!).getByRole("link", { name: "Coding Agents" })).toHaveAttribute("href", "/papers/tasks/coding-agents")
    expect(screen.queryByText("Related Methods")).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /SWE-bench/ }))

    expect(screen.getByText("Benchmark Evidence")).toBeInTheDocument()
    expect(screen.getByText(/recorded benchmark fields/i)).toBeInTheDocument()
    expect(screen.getByText("resolved: 12.5%")).toBeInTheDocument()
    expect(screen.queryByText(/placeholder action/i)).not.toBeInTheDocument()
  })

  it("shows benchmark evidence entries from current matching papers", () => {
    render(
      <BenchmarkEvidencePanel
        benchmark={{
          id: "benchmark-swe-bench",
          slug: "swe-bench",
          name: "SWE-bench",
          category: "software-engineering",
          taskSlug: "coding-agents",
          entryCount: 99
        }}
        context={{ type: "method", method: methodDetail }}
        papers={methodPapers}
        locale="en"
        onClose={vi.fn()}
        onPreviewPaper={vi.fn()}
      />
    )

    expect(screen.getByText("Entries").nextElementSibling).toHaveTextContent("1")
    expect(screen.getByText("resolved: 12.5%")).toBeInTheDocument()
  })

  it("does not render empty method relation panels", () => {
    render(
      <MethodDetailPage
        method={{
          ...methodDetail,
          id: "method-empty",
          slug: "method-empty",
          name: "Empty Method",
          commonBenchmarks: [],
          relatedMethods: [],
          relatedTasks: []
        }}
        locale="en"
        papers={[]}
      />
    )

    expect(screen.queryByText("Related Tasks")).not.toBeInTheDocument()
    expect(screen.queryByText("Related Methods")).not.toBeInTheDocument()
    expect(screen.queryByText("Common Benchmarks")).not.toBeInTheDocument()
  })

  it("derives method detail stats from visible papers without stale taxonomy fallback", () => {
    render(
      <MethodDetailPage
        method={{
          ...methodDetail,
          id: "method-stale-counts",
          slug: "method-stale-counts",
          name: "Method With Stale Counts",
          paperCount: 9,
          taskCount: 7,
          implementationCount: 5,
          commonBenchmarks: [],
          relatedMethods: [],
          relatedTasks: []
        }}
        locale="en"
        papers={[]}
      />
    )

    expect(screen.getByLabelText(/papers: 0/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/tasks: 0/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/implementations: 0/i)).toBeInTheDocument()
    expect(screen.queryByText("Paper With Stale Counts")).not.toBeInTheDocument()
  })

  it("hides method common benchmarks without current paper evidence", () => {
    render(<MethodDetailPage method={methodDetail} locale="en" papers={[]} />)

    expect(screen.getByLabelText(/papers: 0/i)).toBeInTheDocument()
    expect(screen.queryByText("Related Tasks")).not.toBeInTheDocument()
    expect(screen.queryByText("Related Methods")).not.toBeInTheDocument()
    expect(screen.queryByText("Common Benchmarks")).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /SWE-bench/i })).not.toBeInTheDocument()
  })

  it("derives method relation panels from current matching papers", () => {
    render(
      <MethodDetailPage
        method={methodDetail}
        locale="en"
        papers={[
          {
            ...methodPapers[0],
            methodRefs: [
              { id: "method-tool-use", slug: "tool-use", name: "Tool Use" },
              { id: "method-planning", slug: "planning", name: "Planning" }
            ]
          }
        ]}
      />
    )

    const relatedTasksPanel = screen.getByText("Related Tasks").closest("section")
    expect(relatedTasksPanel).not.toBeNull()
    expect(within(relatedTasksPanel!).getByRole("link", { name: "Coding Agents" })).toHaveAttribute("href", "/papers/tasks/coding-agents")
    const relatedMethodsPanel = screen.getByText("Related Methods").closest("section")
    expect(relatedMethodsPanel).not.toBeNull()
    expect(within(relatedMethodsPanel!).getByRole("link", { name: "Planning" })).toHaveAttribute("href", "/papers/methods/planning")
    expect(within(relatedMethodsPanel!).queryByRole("link", { name: "Tool Use" })).not.toBeInTheDocument()
  })

  it("opens benchmark evidence from task detail instead of placeholder copy", () => {
    render(<TaskDetailPage task={taskDetail} locale="en" papers={methodPapers} />)

    fireEvent.click(screen.getByRole("button", { name: /SWE-bench/ }))

    expect(screen.getByText("Benchmark Evidence")).toBeInTheDocument()
    expect(screen.getByText(/recorded benchmark fields on papers in this task/i)).toBeInTheDocument()
    expect(screen.getByText("resolved: 12.5%")).toBeInTheDocument()
    expect(screen.queryByText(/placeholder action/i)).not.toBeInTheDocument()
  })

  it("derives task detail stats from visible papers instead of stale task totals", () => {
    render(
      <TaskDetailPage
        task={{
          ...taskDetail,
          id: "task-custom",
          slug: "custom-task",
          name: "Custom Task",
          benchmarkCount: 9,
          methodCount: 7
        }}
        locale="en"
        papers={[
          {
            ...methodPapers[0],
            id: "paper-custom",
            slug: "paper-custom",
            title: "Custom Paper",
            taskRefs: [{ id: "task-custom", slug: "custom-task", name: "Custom Task" }],
            methodRefs: [],
            benchmarks: []
          }
        ]}
      />
    )

    const hero = screen.getByRole("heading", { name: "Custom Task" }).closest("section")
    expect(hero).not.toBeNull()
    const stats = within(hero!)
      .getAllByText(/^\d+$/)
      .map((node) => node.textContent)
    expect(stats).toEqual(["1", "0", "0"])
    expect(screen.queryByText("Task Branches")).not.toBeInTheDocument()
  })

  it("does not show stale static task benchmarks without current evidence", () => {
    render(
      <TaskDetailPage
        task={{
          ...taskDetail,
          benchmarkCount: 9,
          methodCount: 7
        }}
        locale="en"
        papers={[]}
      />
    )

    const hero = screen.getByRole("heading", { name: "Coding Agents" }).closest("section")
    expect(hero).not.toBeNull()
    const stats = within(hero!)
      .getAllByText(/^\d+$/)
      .map((node) => node.textContent)
    expect(stats).toEqual(["0", "0", "0"])
    expect(screen.queryByRole("button", { name: /SWE-bench/i })).not.toBeInTheDocument()
  })

  it("excludes unpublished papers from task and method detail streams", () => {
    const unpublishedPaper: Paper = {
      ...methodPapers[0],
      id: "paper-draft",
      slug: "paper-draft",
      title: "Draft Paper",
      isPublished: false
    }

    const { unmount } = render(
      <TaskDetailPage task={taskDetail} locale="en" papers={[...methodPapers, unpublishedPaper]} />
    )

    const taskHero = screen.getByRole("heading", { name: "Coding Agents" }).closest("section")
    expect(taskHero).not.toBeNull()
    expect(within(taskHero!).getAllByText("1", { selector: "strong" })).toHaveLength(3)
    expect(screen.getByText("SWE-agent")).toBeInTheDocument()
    expect(screen.queryByText("Draft Paper")).not.toBeInTheDocument()
    unmount()

    render(<MethodDetailPage method={methodDetail} locale="en" papers={[...methodPapers, unpublishedPaper]} />)

    expect(screen.getByLabelText(/papers: 1/i)).toBeInTheDocument()
    expect(screen.getByText("SWE-agent")).toBeInTheDocument()
    expect(screen.queryByText("Draft Paper")).not.toBeInTheDocument()
  })
})
