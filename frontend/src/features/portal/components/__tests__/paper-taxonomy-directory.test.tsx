import { fireEvent, render, screen, within } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { PaperTaxonomyDirectory } from "@/features/portal/components/paper-taxonomy-directory"
import { useUiStore } from "@/stores/ui-store"
import type { Paper } from "@/lib/papers/types"

const replace = vi.fn()
const push = vi.fn()
let search = ""
let pathname = "/papers/methods"

vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
  useRouter: () => ({ replace, push }),
  useSearchParams: () => new URLSearchParams(search)
}))
vi.mock("@/features/portal/components/paper-portal-header", () => ({ PaperPortalHeader: () => <header data-testid="portal-header" /> }))

const papers: Paper[] = [
  {
    id: "paper-tool", slug: "paper-tool", title: "Tool Use Study", abstractSnippet: "Tools.", authors: ["A"], publishedAt: "2026-05-24T00:00:00Z", tags: [],
    taskRefs: [{ id: "task-agent", slug: "agents", name: "Agents" }], methodRefs: [{ id: "method-tool", slug: "tool-use", name: "Tool Use", area: "Agents" }],
    pdfUrl: "https://arxiv.org/pdf/2605.00001.pdf", isPublished: true
  },
  {
    id: "paper-planning", slug: "paper-planning", title: "Planning Study", abstractSnippet: "Plans.", authors: ["B"], publishedAt: "2026-05-23T00:00:00Z", tags: [],
    taskRefs: [{ id: "task-agent", slug: "agents", name: "Agents" }], methodRefs: [{ id: "method-planning", slug: "planning", name: "Planning", area: "Agents" }],
    pdfUrl: "https://arxiv.org/pdf/2605.00002.pdf", isPublished: true
  },
  {
    id: "paper-draft", slug: "paper-draft", title: "Draft Hidden", abstractSnippet: "Draft.", authors: ["C"], publishedAt: "2026-05-22T00:00:00Z", tags: [],
    taskRefs: [{ id: "task-agent", slug: "agents", name: "Agents" }], methodRefs: [{ id: "method-tool", slug: "tool-use", name: "Tool Use", area: "Agents" }],
    pdfUrl: "https://arxiv.org/pdf/2605.00003.pdf", isPublished: false
  }
]

const definitions = {
  methods: [
    { slug: "tool-use", name: "Tool Use", nameZh: "工具使用", group: "Agents", description: "Using tools.", descriptionZh: "使用工具。" },
    { slug: "planning", name: "Planning", nameZh: "规划", group: "Agents", description: "Planning.", descriptionZh: "规划。" },
    { slug: "reasoning", name: "Reasoning", nameZh: "推理", group: "Reasoning", description: "Reasoning.", descriptionZh: "推理。" },
    { slug: "memory", name: "Memory", nameZh: "记忆", group: "Agents", description: "Memory.", descriptionZh: "记忆。" },
    { slug: "retrieval", name: "Retrieval", nameZh: "检索", group: "Knowledge", description: "Retrieval.", descriptionZh: "检索。" },
    { slug: "verification", name: "Verification", nameZh: "验证", group: "Reasoning", description: "Verification.", descriptionZh: "验证。" },
    { slug: "alignment", name: "Alignment", nameZh: "对齐", group: "Evaluation", description: "Alignment.", descriptionZh: "对齐。" },
    { slug: "language-model", name: "Language Model", nameZh: "语言模型", group: "Language Models", description: "Language models.", descriptionZh: "语言模型。" },
    { slug: "attention", name: "Attention", nameZh: "注意力", group: "Transformers", description: "Attention.", descriptionZh: "注意力。" }
  ],
  tasks: [{ slug: "agents", name: "Agents", nameZh: "智能体", group: "agents", description: "Agents.", descriptionZh: "智能体。" }]
}

describe("PaperTaxonomyDirectory", () => {
  beforeEach(() => {
    search = ""
    pathname = "/papers/methods"
    replace.mockReset()
    push.mockReset()
    useUiStore.setState({ locale: "zh" })
  })

  it("shows five directions first and expands the remaining direction", () => {
    render(<PaperTaxonomyDirectory kind="method" papers={papers} definitions={definitions} source="cache" />)
    expect(screen.getByRole("heading", { name: "研究方法." })).toBeInTheDocument()
    const sidebar = screen.getByRole("complementary", { name: "方向筛选" })
    expect(within(sidebar).queryByRole("button", { name: "Transformer 架构1" })).not.toBeInTheDocument()
    fireEvent.click(within(sidebar).getByRole("button", { name: "展开更多方向 (1)" }))
    expect(within(sidebar).getByRole("button", { name: "Transformer 架构1" })).toBeInTheDocument()
    fireEvent.click(within(sidebar).getByRole("button", { name: "收起方向" }))
    expect(within(sidebar).queryByRole("button", { name: "Transformer 架构1" })).not.toBeInTheDocument()
    expect(screen.getAllByText("论文待关联").length).toBeGreaterThan(0)
  })

  it("filters by query and area, sorts by name, and writes the URL state", () => {
    search = "group=Agents&sort=name"
    render(<PaperTaxonomyDirectory kind="method" papers={papers} definitions={definitions} source="backend" />)
    expect(screen.getAllByRole("heading", { level: 3 }).map(heading => heading.textContent)).toEqual(["工具使用", "规划", "记忆"])
    fireEvent.change(screen.getByRole("textbox", { name: "搜索研究方法" }), { target: { value: "tool" } })
    fireEvent.submit(screen.getByRole("search"))
    expect(replace).toHaveBeenLastCalledWith("/papers/methods?group=Agents&sort=name&q=tool", { scroll: false })
  })

  it("updates the category URL without losing the current directory path", () => {
    search = "group=Agents&sort=name"
    render(<PaperTaxonomyDirectory kind="method" papers={papers} definitions={definitions} source="cache" />)
    fireEvent.click(screen.getByRole("link", { name: /工具使用/ }))
    expect(push).toHaveBeenLastCalledWith("/papers/methods?group=Agents&sort=name&category=tool-use", { scroll: false })
  })

  it("opens an annotated definition, excludes drafts, and preserves safe return context", () => {
    search = "q=tool&group=Agents&category=tool-use"
    render(<PaperTaxonomyDirectory kind="method" papers={papers} definitions={definitions} source="cache" />)
    expect(screen.getByText("关联论文")).toBeInTheDocument()
    expect(screen.getByText("1 篇")).toBeInTheDocument()
    const readerHref = screen.getByRole("link", { name: /Tool Use Study/ }).getAttribute("href")
    const readerUrl = new URL(readerHref!, "http://localhost")
    expect(readerUrl.pathname).toBe("/papers/paper-tool/read")
    expect(readerUrl.searchParams.get("returnTo")).toBe("/papers/methods?q=tool&group=Agents&category=tool-use")
    expect(screen.queryByText("Draft Hidden")).not.toBeInTheDocument()
  })

  it("opens a pending definition without inventing a paper count", () => {
    search = "category=memory"
    render(<PaperTaxonomyDirectory kind="method" papers={[]} definitions={definitions} source="empty" />)
    expect(screen.getByRole("heading", { name: "记忆" })).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "还没有已标注的关联论文" })).toBeInTheDocument()
    expect(screen.queryByText(/\d+ 篇关联论文/)).not.toBeInTheDocument()
    expect(screen.getByRole("link", { name: "用名称检索论文" })).toHaveAttribute("href", "/design-demo/papers?q=Memory")
  })

  it("supports task definitions while keeping zero-association categories explicit", () => {
    pathname = "/papers/tasks"
    render(<PaperTaxonomyDirectory kind="task" papers={papers} definitions={{ methods: [], tasks: [
      { slug: "agents", name: "Agents", nameZh: "智能体", group: "agents", description: "Agents.", descriptionZh: "智能体。" },
      { slug: "summarization", name: "Summarization", nameZh: "摘要", group: "language-models", description: "Summaries.", descriptionZh: "摘要。" }
    ] }} source="empty" />)
    const region = screen.getByRole("region", { name: "研究任务" })
    expect(within(region).getByText("论文待关联")).toBeInTheDocument()
    expect(within(region).getByText("2 篇关联论文")).toBeInTheDocument()
  })
})
