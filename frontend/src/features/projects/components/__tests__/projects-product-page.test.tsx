import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useUiStore } from "@/stores/ui-store"
const navigation = vi.hoisted(() => ({ query: "", replace: vi.fn() }))
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(navigation.query),
  usePathname: () => "/projects",
  useRouter: () => ({ replace: navigation.replace }),
}))
import { LabGraph, LabSolutionPanel, ProjectsProductPage } from "@/features/projects/components/projects-product-page"
import {
  addProjectWatchlistItem,
  answerProjectLabQuestion,
  fetchProjectProductSection,
  fetchProjectsHome,
  explainProjectLabNode,
  generateProjectLabSolution,
  recordProjectInteraction,
  startProjectLabSession,
  ProjectsApiError,
} from "@/lib/projects/api"

vi.mock("@/lib/projects/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/projects/api")>()
  return {
    ...actual,
    addProjectWatchlistItem: vi.fn(),
    answerProjectLabQuestion: vi.fn(),
    fetchProjectProductSection: vi.fn(),
    fetchProjectsHome: vi.fn(),
    explainProjectLabNode: vi.fn(),
    generateProjectLabSolution: vi.fn(),
    recordProjectInteraction: vi.fn(),
    startProjectLabSession: vi.fn(),
  }
})

describe("ProjectsProductPage", () => {
  beforeEach(() => {
    useUiStore.setState({ locale: "en" })
    navigation.query = ""
    navigation.replace.mockReset()
  })

  afterEach(() => {
    vi.mocked(addProjectWatchlistItem).mockReset()
    vi.mocked(answerProjectLabQuestion).mockReset()
    vi.mocked(fetchProjectsHome).mockReset()
    vi.mocked(explainProjectLabNode).mockReset()
    vi.mocked(fetchProjectProductSection).mockReset()
    vi.mocked(generateProjectLabSolution).mockReset()
    vi.mocked(recordProjectInteraction).mockReset()
    vi.mocked(startProjectLabSession).mockReset()
  })

  it("renders the Projects product home from API v1 data", async () => {
    vi.mocked(fetchProjectsHome).mockResolvedValueOnce({
      hot: [project("p1", "AgentKit")],
      rising: [],
      tools: [],
      cases: [{ id: "case-1", project_id: "p1", title: "Agent workflow case", business_domain: "engineering", module_type: "workflow" }],
      collections: [{ id: "collection-1", slug: "agents", title: "Agent Projects", description: "Real projects.", item_count: 1 }],
      watchlist: [],
      recommendations: [],
      meta: { source: "artifact", source_run_id: "run-project-radar", data_state: "ready", notices: [] },
      metrics: [],
    })

    renderWithQueryClient(<ProjectsProductPage route="home" />)

    expect(await screen.findByRole("heading", { name: "Projects" })).toBeInTheDocument()
    expect(screen.getAllByText("AgentKit").length).toBeGreaterThan(0)
    expect(screen.getByText("Agent workflow case")).toBeInTheDocument()
    expect(fetchProjectsHome).toHaveBeenCalledWith({ limit: 6 })
  })

  it("uses the homepage query for a real project search and keeps the original question visible", async () => {
    navigation.query = "question=%E6%89%BE%E4%B8%80%E4%B8%AA+Agent+%E9%A1%B9%E7%9B%AE&q=Agent&source=home"
    vi.mocked(fetchProjectProductSection).mockResolvedValueOnce({
      items: [project("p-agent", "AgentProject")],
      page: { page: 1, page_size: 18, total: 1, has_next: false },
      meta: { source: "artifact", data_state: "ready", notices: [] },
      metrics: [],
    })

    renderWithQueryClient(<ProjectsProductPage route="home" />)

    expect(await screen.findByText("找一个 Agent 项目")).toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: "Search projects" })).toHaveValue("Agent")
    expect(fetchProjectProductSection).toHaveBeenCalledWith("hot", {
      params: { q: "Agent", limit: 18, category: undefined },
    })
    expect(fetchProjectsHome).not.toHaveBeenCalled()
  })

  it("keeps an explicit empty q when a homepage query is cleared", async () => {
    navigation.query = "question=%E6%89%BE%E4%B8%80%E4%B8%AA+Agent+%E9%A1%B9%E7%9B%AE&q=Agent&source=home"
    vi.mocked(fetchProjectProductSection).mockResolvedValueOnce({
      items: [],
      page: { page: 1, page_size: 18, total: 0, has_next: false },
      meta: { source: "none", data_state: "empty", notices: [] },
      metrics: [],
    })

    renderWithQueryClient(<ProjectsProductPage route="home" />)
    const input = await screen.findByRole("textbox", { name: "Search projects" })
    fireEvent.change(input, { target: { value: "" } })
    fireEvent.click(screen.getByRole("button", { name: "搜索" }))

    expect(navigation.replace).toHaveBeenCalledWith(
      "/projects?question=%E6%89%BE%E4%B8%80%E4%B8%AA+Agent+%E9%A1%B9%E7%9B%AE&q=&source=home",
      { scroll: false },
    )
  })

  it("renders explicit empty state when no real Project Radar data exists", async () => {
    vi.mocked(fetchProjectProductSection).mockResolvedValueOnce({
      items: [],
      page: { page: 1, page_size: 18, total: 0, has_next: false },
      meta: { source: "none", data_state: "empty", notices: ["No real Project Radar artifacts were found."] },
      metrics: [],
    })

    renderWithQueryClient(<ProjectsProductPage route="hot" />)

    await waitFor(() => expect(fetchProjectProductSection).toHaveBeenCalled())
    expect(await screen.findByText("No Real Project Radar Data")).toBeInTheDocument()
    expect(screen.getAllByText(/will not substitute fake projects/i).length).toBeGreaterThan(0)
  })

  it("renders a degraded notice while retaining parseable real Project Radar data", async () => {
    vi.mocked(fetchProjectProductSection).mockResolvedValueOnce({
      items: [project("p-degraded", "PartialKit")],
      page: { page: 1, page_size: 18, total: 1, has_next: false },
      meta: { source: "artifact", source_run_id: "run-partial", data_state: "partial", notices: ["Some source records were not parseable."] },
      metrics: [],
    })

    renderWithQueryClient(<ProjectsProductPage route="hot" />)

    expect((await screen.findAllByText("PartialKit")).length).toBeGreaterThan(0)
    expect(screen.getByText("Some source records were not parseable.")).toBeInTheDocument()
    expect(screen.getByText("Partial")).toBeInTheDocument()
  })

  it("uses a workspace-shaped skeleton while Lab data is loading", () => {
    vi.mocked(fetchProjectProductSection).mockImplementationOnce(() => new Promise(() => undefined))

    renderWithQueryClient(<ProjectsProductPage route="lab" />)

    expect(screen.getByLabelText("Loading Projects Lab workspace")).toHaveAttribute("aria-busy", "true")
    expect(screen.queryByText("Loading Lab")).not.toBeInTheDocument()
  })

  it("uses the research page frame without the generic Projects focus hero", async () => {
    vi.mocked(fetchProjectProductSection).mockResolvedValueOnce({
      hot: [],
      rising: [],
      tools: [],
      cases: [],
      collections: [],
      watchlist: [],
      recommendations: [],
      meta: { source: "none", data_state: "empty", notices: ["No real Project Radar artifacts were found."] },
      metrics: [],
    })

    renderWithQueryClient(<ProjectsProductPage route="lab" />)

    expect(await screen.findByRole("heading", { name: "Lab" })).toBeInTheDocument()
    expect(screen.getByText("Research workspace")).toBeInTheDocument()
    expect(screen.queryByText("Current focus")).not.toBeInTheDocument()
    expect(screen.getByText("Cases")).toBeInTheDocument()
  })

  it("starts a Lab session, answers a question, and renders the generated solution", async () => {
    vi.mocked(fetchProjectProductSection).mockResolvedValueOnce({
      hot: [project("p1", "AgentKit")],
      rising: [],
      tools: [],
      cases: [
        { id: "case-1", project_id: "p1", title: "Agent workflow case", business_domain: "engineering", module_type: "workflow" },
        { id: "case-2", project_id: "p1", title: "Retrieval case", business_domain: "research", module_type: "rag" },
        { id: "case-3", project_id: "p1", title: "Ops case", business_domain: "operations", module_type: "monitoring" },
        { id: "case-4", project_id: "p1", title: "Extra case", business_domain: "support", module_type: "workflow" },
      ],
      collections: [],
      watchlist: [],
      recommendations: [],
      meta: readyMeta(),
      metrics: [],
    })
    vi.mocked(startProjectLabSession).mockResolvedValueOnce({
      session: {
        id: "lab-session-1",
        user_problem: "Need a newsroom workflow module",
        selected_case_ids: ["case-1", "case-2", "case-3"],
        current_stage: "clarifying_requirements",
        next_action: "answer_question",
        can_generate_solution: false,
        unanswered_question_ids: ["q-context", "q-deploy"],
        questions: [
          { id: "q-context", question: "Which workflow stage needs the most help?", required: true },
          { id: "q-deploy", question: "What deployment constraint matters most?", required: true },
        ],
      },
    })
    vi.mocked(answerProjectLabQuestion)
      .mockResolvedValueOnce({
        session: {
          id: "lab-session-1",
          user_problem: "Need a newsroom workflow module",
          selected_case_ids: ["case-1", "case-2", "case-3"],
          current_stage: "clarifying_requirements",
          next_action: "answer_question",
          can_generate_solution: false,
          unanswered_question_ids: ["q-deploy"],
          questions: [
            { id: "q-context", question: "Which workflow stage needs the most help?", required: true, answered_value: "Batch ingestion" },
            { id: "q-deploy", question: "What deployment constraint matters most?", required: true },
          ],
        },
      })
      .mockResolvedValueOnce({
        session: {
          id: "lab-session-1",
          user_problem: "Need a newsroom workflow module",
          selected_case_ids: ["case-1", "case-2", "case-3"],
          current_stage: "ready_to_generate",
          next_action: "generate_solution",
          can_generate_solution: true,
          unanswered_question_ids: [],
          questions: [
            { id: "q-context", question: "Which workflow stage needs the most help?", required: true, answered_value: "Batch ingestion" },
            { id: "q-deploy", question: "What deployment constraint matters most?", required: true, answered_value: "Local deployment" },
          ],
        },
      })
    vi.mocked(generateProjectLabSolution).mockResolvedValueOnce({
      session: {
        id: "lab-session-1",
        user_problem: "Need a newsroom workflow module",
        selected_case_ids: ["case-1", "case-2", "case-3"],
        current_stage: "solution_generated",
        next_action: "review_solution",
        can_generate_solution: false,
        unanswered_question_ids: [],
        questions: [
          { id: "q-context", question: "Which workflow stage needs the most help?", required: true, answered_value: "Batch ingestion" },
          { id: "q-deploy", question: "What deployment constraint matters most?", required: true, answered_value: "Local deployment" },
        ],
        generated_solution: "Use AgentKit as the orchestration layer.",
      },
      solution: {
        module: "workflow automation",
        candidate_projects: ["AgentKit"],
      },
    })

    renderWithQueryClient(<ProjectsProductPage route="lab" />)

    const startButton = await screen.findByRole("button", { name: "Start Session" })
    expect(startButton).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText("Describe the module or product problem"), {
      target: { value: "  Need a newsroom workflow module  " },
    })
    expect(startButton).toBeEnabled()
    fireEvent.click(startButton)

    await waitFor(() =>
      expect(startProjectLabSession).toHaveBeenCalledWith({
        user_problem: "Need a newsroom workflow module",
        selected_case_ids: ["case-1", "case-2", "case-3"],
      })
    )
    expect(await screen.findByText("clarifying_requirements")).toBeInTheDocument()
    expect(screen.getByText("Which workflow stage needs the most help?")).toBeInTheDocument()

    const answerButton = screen.getByRole("button", { name: "Answer" })
    expect(answerButton).toBeDisabled()
    fireEvent.change(screen.getByPlaceholderText("Answer clarification"), { target: { value: "  Batch ingestion  " } })
    expect(answerButton).toBeEnabled()
    fireEvent.click(answerButton)

    await waitFor(() =>
      expect(answerProjectLabQuestion).toHaveBeenCalledWith("lab-session-1", {
        question_id: "q-context",
        answer: "Batch ingestion",
      })
    )
    expect(await screen.findByText("clarifying_requirements")).toBeInTheDocument()
    expect(screen.getByText("What deployment constraint matters most?")).toBeInTheDocument()
    expect(screen.getByPlaceholderText("Answer clarification")).toHaveValue("")
    expect(screen.getByRole("button", { name: "Generate Solution" })).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText("Answer clarification"), { target: { value: "  Local deployment  " } })
    fireEvent.click(screen.getByRole("button", { name: "Answer" }))

    await waitFor(() =>
      expect(answerProjectLabQuestion).toHaveBeenLastCalledWith("lab-session-1", {
        question_id: "q-deploy",
        answer: "Local deployment",
      })
    )
    expect(await screen.findByText("ready_to_generate")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Generate Solution" })).toBeEnabled()

    fireEvent.click(screen.getByRole("button", { name: "Generate Solution" }))

    await waitFor(() => expect(generateProjectLabSolution).toHaveBeenCalledWith("lab-session-1"))
    expect(await screen.findByText(/workflow automation/)).toBeInTheDocument()
    expect(screen.getByText(/candidate_projects/)).toBeInTheDocument()
  })

  it("keeps the brief and exposes pending feedback when starting the session fails", async () => {
    vi.mocked(fetchProjectProductSection).mockResolvedValueOnce({
      hot: [], rising: [], tools: [], cases: [], collections: [], watchlist: [], recommendations: [], meta: readyMeta(), metrics: [],
    })
    vi.mocked(startProjectLabSession).mockRejectedValueOnce(new Error("Session service unavailable"))

    renderWithQueryClient(<ProjectsProductPage route="lab" />)
    const brief = await screen.findByPlaceholderText("Describe the module or product problem")
    fireEvent.change(brief, { target: { value: "  Keep this failed brief  " } })
    fireEvent.click(screen.getByRole("button", { name: "Start Session" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("Session service unavailable")
    expect(brief).toHaveValue("  Keep this failed brief  ")
  })

  it("shows pending feedback while a session request is in flight", async () => {
    vi.mocked(fetchProjectProductSection).mockResolvedValueOnce({
      hot: [], rising: [], tools: [], cases: [], collections: [], watchlist: [], recommendations: [], meta: readyMeta(), metrics: [],
    })
    vi.mocked(startProjectLabSession).mockImplementationOnce(() => new Promise(() => undefined))

    renderWithQueryClient(<ProjectsProductPage route="lab" />)
    const brief = await screen.findByPlaceholderText("Describe the module or product problem")
    fireEvent.change(brief, { target: { value: "Need pending feedback" } })
    fireEvent.click(screen.getByRole("button", { name: "Start Session" }))

    expect(await screen.findByText("Creating session from real data")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Starting" })).toBeDisabled()
  })

  it("renders a readiness response when generation is rejected with 409", async () => {
    vi.mocked(fetchProjectProductSection).mockResolvedValueOnce({
      hot: [], rising: [], tools: [], cases: [], collections: [], watchlist: [], recommendations: [], meta: readyMeta(), metrics: [],
    })
    vi.mocked(startProjectLabSession).mockResolvedValueOnce({ session: {
      id: "lab-session-409", user_problem: "Need a gated workflow", selected_case_ids: [], current_stage: "ready_to_generate", next_action: "generate_solution", can_generate_solution: true, unanswered_question_ids: [], questions: [],
    } })
    vi.mocked(generateProjectLabSolution).mockRejectedValueOnce(new ProjectsApiError("Answer required", "lab_not_ready", { unanswered_question_ids: ["q-context"] }, false, { status: 409, userActionRequired: true }))

    renderWithQueryClient(<ProjectsProductPage route="lab" />)
    const brief = await screen.findByPlaceholderText("Describe the module or product problem")
    fireEvent.change(brief, { target: { value: "Need a gated workflow" } })
    fireEvent.click(screen.getByRole("button", { name: "Start Session" }))
    const generate = await screen.findByRole("button", { name: "Generate Solution" })
    fireEvent.click(generate)

    expect(await screen.findByRole("alert")).toHaveTextContent("Return to the unanswered clarification above.")
  })

  it("reports structured solution copy success and failure", async () => {
    const writeText = vi.fn().mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error("Clipboard blocked"))
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } })
    const solutionSession = {
      id: "lab-session-copy", user_problem: "Need copyable output", selected_case_ids: [], current_stage: "solution_generated" as const, next_action: "review_solution" as const, can_generate_solution: false, unanswered_question_ids: [], questions: [], solution_json: { module: "workflow" },
    }

    const view = renderWithQueryClient(<LabSolutionPanel session={solutionSession} solution={{ module: "workflow" }} />)
    const structuredTab = screen.getByRole("tab", { name: "Structured" })
    structuredTab.focus()
    fireEvent.keyDown(structuredTab, { key: "Enter", code: "Enter" })
    await waitFor(() => expect(screen.getByRole("tab", { name: "Structured" })).toHaveAttribute("aria-selected", "true"))
    const copy = screen.getByRole("button", { name: "Copy structured solution data" })
    fireEvent.click(copy)
    expect(await screen.findByText("Structured solution data copied.")).toBeInTheDocument()
    fireEvent.click(copy)
    expect(await screen.findByText("Structured solution data could not be copied.")).toBeInTheDocument()
    expect(writeText).toHaveBeenCalledTimes(2)
    view.unmount()
  })

  it("keeps Lab actions disabled when the server returns an unsupported stage", async () => {
    vi.mocked(fetchProjectProductSection).mockResolvedValueOnce({
      hot: [],
      rising: [],
      tools: [],
      cases: [],
      collections: [],
      watchlist: [],
      recommendations: [],
      meta: readyMeta(),
      metrics: [],
    })
    vi.mocked(startProjectLabSession).mockResolvedValueOnce({
      session: {
        id: "lab-session-unknown",
        user_problem: "Need a future workflow",
        selected_case_ids: [],
        current_stage: "unknown",
        raw_current_stage: "future_stage",
        next_action: "unknown",
        can_generate_solution: true,
        unanswered_question_ids: [],
        questions: [],
      },
    })

    renderWithQueryClient(<ProjectsProductPage route="lab" />)
    fireEvent.change(await screen.findByPlaceholderText("Describe the module or product problem"), {
      target: { value: "Need a future workflow" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Start Session" }))

    expect(await screen.findByText("Unsupported stage: future_stage")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Generate Solution" })).toBeDisabled()
  })

  it("exposes graph relationships and isolates node explanation feedback", async () => {
    vi.mocked(explainProjectLabNode).mockResolvedValueOnce({
      title: "User problem",
      explanation: "The requirement is connected to the selected case.",
      related_nodes: [{ id: "case-1" }],
    })

    renderWithQueryClient(
      <LabGraph
        sessionId="lab-session-graph"
        graph={{
          nodes: [
            { id: "problem-1", title: "User problem", node_type: "user_problem" },
            { id: "case-1", title: "Selected case", node_type: "case" },
          ],
          edges: [{ source_id: "problem-1", target_id: "case-1" }],
        }}
      />
    )

    expect(screen.getByRole("img", { name: "Graph with 2 nodes and 1 relationships" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /User problem.*user_problem.*1 related/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Explain User problem" }))

    await waitFor(() => expect(explainProjectLabNode).toHaveBeenCalledWith("lab-session-graph", { node_id: "problem-1", style: "plain" }))
    expect(await screen.findByText("The requirement is connected to the selected case.")).toBeInTheDocument()

    vi.mocked(explainProjectLabNode).mockRejectedValueOnce(new Error("Explanation unavailable"))
    fireEvent.click(screen.getByRole("button", { name: "Explain Selected case" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("Explanation unavailable")
  })

  it("adds a Watchlist item and refreshes the list after a successful save", async () => {
    const savedItem = {
      id: "watch-1",
      project_id: "project-watch-1",
      watch_reason: "Track release cadence",
      priority: "high" as const,
      status: "active" as const,
    }
    vi.mocked(fetchProjectProductSection)
      .mockResolvedValueOnce({
        items: [],
        meta: readyMeta(),
      })
      .mockResolvedValueOnce({
        items: [savedItem],
        meta: readyMeta(),
      })
    vi.mocked(addProjectWatchlistItem).mockResolvedValueOnce({ item: savedItem })

    renderWithQueryClient(<ProjectsProductPage route="watchlist" />)

    const watchButton = await screen.findByRole("button", { name: "Watch Project" })
    expect(watchButton).toBeDisabled()
    expect(screen.getByText("No Watchlist Items")).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText("Real project id"), { target: { value: "  project-watch-1  " } })
    fireEvent.change(screen.getByPlaceholderText("Watch reason"), { target: { value: "  Track release cadence  " } })
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "high" } })
    expect(watchButton).toBeEnabled()
    fireEvent.click(watchButton)

    await waitFor(() =>
      expect(addProjectWatchlistItem).toHaveBeenCalledWith({
        project_id: "project-watch-1",
        watch_reason: "Track release cadence",
        priority: "high",
      })
    )
    await waitFor(() => expect(fetchProjectProductSection).toHaveBeenCalledTimes(2))
    expect(await screen.findByText("project-watch-1")).toBeInTheDocument()
    expect(screen.getByPlaceholderText("Real project id")).toHaveValue("")
    expect(screen.getByPlaceholderText("Watch reason")).toHaveValue("")
    expect(screen.getByRole("combobox")).toHaveValue("medium")
  })
})

function renderWithQueryClient(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>)
}

function project(id: string, name: string) {
  return {
    id,
    slug: id,
    name,
    description: "Real Project Radar project.",
    github_url: "https://github.com/acme/agentkit",
    project_type: "tool",
    tags: ["agent"],
    source_confidence: 0.9,
    hot_score: 0.8,
    metric_summary: { github_stars: 100, github_forks: 5, stars_delta_7d: 10 },
    capability_count: 1,
    case_count: 1,
    source_count: 1,
  }
}

function readyMeta() {
  return { source: "artifact" as const, source_run_id: "run-project-radar", data_state: "ready" as const, notices: [] }
}
