import { StrictMode } from "react"
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { DesignDemoPage } from "../design-demo-page"
import { readResearchWorkspace, recordResearchVisit, saveResearchGroup, saveResearchMaterial, selectHistoryOwner } from "@/lib/research/history"
import { researchQuestionHref } from "@/lib/research/entry"

const push = vi.hoisted(() => vi.fn())
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }), useSearchParams: () => new URLSearchParams(window.location.search) }))
vi.mock("@/components/auth/portal-account-control", () => ({ PortalAccountControl: () => <button>登录</button> }))
const intent = { summary: "Agent 如何完成任务", query: "Agent", sources: ["papers", "projects"], constraints: {}, clarification: null }
const search = (source: string) => ({ source, results: [{ id: source === "papers" ? "paper-a" : "project-a", kind: source, title: source === "papers" ? "Agent 实验论文" : "Agent 开源工具", description: "关于 Agent 任务的来源内容。", source: source === "papers" ? "arXiv" : "GitHub", url: source === "papers" ? "https://arxiv.org/abs/2605.22343" : "https://github.com/example/agent", href: source === "papers" ? "/design-demo/papers/paper-a/read" : "/projects/project-a" }], total: 1, moreHref: source === "papers" ? "/design-demo/papers?q=Agent" : "/projects?q=Agent" })
const success = (data: unknown) => new Response(JSON.stringify({ success: true, data }), { headers: { "Content-Type": "application/json" } })
const send = (question = "找 Agent 论文和项目") => { fireEvent.change(screen.getByRole("textbox", { name: "向 Agora AI 提问" }), { target: { value: question } }); fireEvent.click(screen.getByRole("button", { name: "发送问题" })) }

describe("guided research homepage", () => {
  beforeEach(() => {
    localStorage.clear(); sessionStorage.clear(); selectHistoryOwner(null); window.history.replaceState(null, "", "/design-demo"); push.mockReset()
    Element.prototype.scrollIntoView = vi.fn(); window.matchMedia = vi.fn().mockReturnValue({ matches: false }); global.requestAnimationFrame = callback => { callback(0); return 0 }
    vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => path.endsWith("intent") ? success(intent) : success(search(JSON.parse(String(init.body)).source))))
  })
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it("fetches both real source contracts and keeps results in one research", async () => {
    render(<DesignDemoPage />)
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled()
    expect(screen.queryByText("准备回答")).not.toBeInTheDocument()
    send()
    await screen.findByRole("region", { name: "论文查找结果" }); await screen.findByRole("region", { name: "开源项目查找结果" })
    expect(push).not.toHaveBeenCalled()
    expect(screen.getByRole("link", { name: "打开 GitHub" })).toHaveAttribute("href", "https://github.com/example/agent")
    expect(screen.getByRole("link", { name: "阅读全文" }).getAttribute("href")).toContain("returnTo=%2Fdesign-demo%3FresearchSession%3D")
    expect(readResearchWorkspace().visits).toHaveLength(1)
    expect(readResearchWorkspace().visits[0].conversation?.turns[0].searches).toHaveLength(2)
  })

  it("only searches after plan confirmation; type buttons are choices", async () => {
    sessionStorage.setItem("agora-home-draft:v1", JSON.stringify({ query: "想了解 Agent", mode: "plan" }))
    render(<DesignDemoPage />); fireEvent.click(screen.getByRole("button", { name: "发送问题" }))
    const card = await screen.findByRole("region", { name: "确认研究计划" })
    fireEvent.click(within(card).getByRole("button", { name: "论文", exact: true }))
    expect(within(card).getByRole("button", { name: "论文", exact: true })).toHaveAttribute("aria-pressed", "true")
    expect(fetch).toHaveBeenCalledTimes(1)
    fireEvent.click(within(card).getByRole("button", { name: "都看看" })); fireEvent.click(within(card).getByRole("button", { name: "开始查找" }))
    await screen.findByRole("link", { name: "打开 GitHub" }); expect(fetch).toHaveBeenCalledTimes(3)
  })

  it("bounds clarification to two answers even if a candidate keeps asking", async () => {
    vi.mocked(fetch).mockImplementation(async () => success({ ...intent, clarification: { question: "你想先了解，还是找项目试试？", options: ["先了解一下", "找项目试试"] } }))
    sessionStorage.setItem("agora-home-draft:v1", JSON.stringify({ query: "Agent", mode: "plan" }))
    render(<DesignDemoPage />); fireEvent.click(screen.getByRole("button", { name: "发送问题" }))
    fireEvent.click(await screen.findByRole("button", { name: "先了解一下" })); fireEvent.click(await screen.findByRole("button", { name: "找项目试试" }))
    await screen.findByRole("region", { name: "确认研究计划" })
    expect(readResearchWorkspace().visits[0].conversation?.turns[0].answers).toEqual(["先了解一下", "找项目试试"])
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls.at(-1)?.[1]?.body)).skipClarification).toBe(true)
    expect(screen.queryByRole("button", { name: "先了解一下" })).not.toBeInTheDocument()
  })

  it("retains papers while retrying only a failed project source", async () => {
    let failed = false
    vi.mocked(fetch).mockImplementation(async (path, init) => {
      if (String(path).endsWith("intent")) return success(intent)
      const { source } = JSON.parse(String(init?.body))
      if (source === "projects" && !failed) { failed = true; return new Response(JSON.stringify({ success: false, error: { message: "项目暂时不可用" } }), { status: 503 }) }
      return success(search(source))
    })
    render(<DesignDemoPage />); send(); await screen.findByRole("link", { name: "阅读全文" })
    fireEvent.click(await screen.findByRole("button", { name: "重试开源项目" })); await screen.findByRole("link", { name: "打开 GitHub" })
    expect(vi.mocked(fetch).mock.calls.filter(call => String(call[0]).endsWith("search") && JSON.parse(String(call[1]?.body)).source === "papers")).toHaveLength(1)
    expect(readResearchWorkspace().visits).toHaveLength(1)
  })

  it("saves follow-ups in one visit and restores results without repeating requests", async () => {
    const view = render(<StrictMode><DesignDemoPage /></StrictMode>); send(); await screen.findByRole("link", { name: "打开 GitHub" })
    fireEvent.change(screen.getByRole("textbox", { name: "继续这次研究" }), { target: { value: "项目要能在本地运行" } }); fireEvent.click(screen.getByRole("button", { name: "发送补充" }))
    await waitFor(() => expect(readResearchWorkspace().visits[0].conversation?.turns[1].phase).toBe("results"))
    expect(readResearchWorkspace().visits).toHaveLength(1)
    expect(readResearchWorkspace().visits[0].conversation?.turns).toHaveLength(2)
    const followup = vi.mocked(fetch).mock.calls.filter(call => String(call[0]).endsWith("intent")).at(-1)
    expect(JSON.parse(String(followup?.[1]?.body)).previous[0].searches).toHaveLength(2)
    view.unmount(); vi.mocked(fetch).mockClear(); render(<DesignDemoPage />)
    expect(screen.getAllByRole("link", { name: "打开 GitHub" })).toHaveLength(2); expect(fetch).not.toHaveBeenCalled()
  })

  it("discards late completions after stopping and starting new research", async () => {
    let resolve: (value: Response) => void = () => {}
    vi.mocked(fetch).mockImplementation(() => new Promise(done => { resolve = done }))
    render(<DesignDemoPage />); send(); fireEvent.click(await screen.findByRole("button", { name: "停止" }))
    expect(screen.getByText("查找已暂停，已有内容已保留。")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "新研究" })); await act(async () => resolve(success(intent)))
    expect(screen.getByRole("textbox", { name: "向 Agora AI 提问" })).toHaveValue("")
    expect(screen.queryByRole("region", { name: "确认研究计划" })).not.toBeInTheDocument()
    expect(readResearchWorkspace().visits[0].conversation?.turns[0].phase).toBe("stopped")
  })

  it("hides previous account content before its response returns", async () => {
    let resolve: (value: Response) => void = () => {}
    vi.mocked(fetch).mockImplementation(() => new Promise(done => { resolve = done }))
    render(<DesignDemoPage />); send("属于原账号的研究"); act(() => selectHistoryOwner("new-owner")); await act(async () => resolve(success(intent)))
    expect(screen.queryByText("属于原账号的研究")).not.toBeInTheDocument(); expect(readResearchWorkspace().visits).toHaveLength(0)
  })

  it("separates selected owned material IDs from the storage group", async () => {
    saveResearchGroup("Agent 分组", "group-a")
    saveResearchMaterial({ id: "source-a", groupId: "group-a", kind: "paper", title: "已选论文", url: "https://arxiv.org/abs/2605.22343", notes: "", createdAt: 1, updatedAt: 2 })
    window.history.replaceState(null, "", "/design-demo?material=source-a&material=not-owned&researchGroup=group-a")
    render(<DesignDemoPage />); send(); await screen.findByRole("link", { name: "阅读全文" })
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body)).publicSources).toEqual([expect.objectContaining({ id: "source-a", url: "https://arxiv.org/abs/2605.22343" })])
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body)).materialIds).toEqual([])
    expect(readResearchWorkspace().visits[0].groupId).toBe("group-a")
  })

  it("preserves legacy module resume", () => {
    recordResearchVisit(researchQuestionHref("papers", "旧研究") + "&sort=citations", 320); render(<DesignDemoPage />)
    fireEvent.click(screen.getByRole("button", { name: "继续研究：旧研究" })); expect(push).toHaveBeenCalledWith(expect.stringContaining("sort=citations"), { scroll: false })
  })

  it("requires confirmation before an automatic follow-up relaxes saved conditions", async () => {
    render(<DesignDemoPage />); send(); await screen.findByRole("link", { name: "打开 GitHub" })
    vi.mocked(fetch).mockImplementation(async (path, init) => String(path).endsWith("intent") ? success({ ...intent, confirmationRequired: true, changeNotice: "时间从最近一年改为不限。" }) : success(search(JSON.parse(String(init?.body)).source)))
    const previousSearchCount = vi.mocked(fetch).mock.calls.filter(call => String(call[0]).endsWith("search")).length
    fireEvent.change(screen.getByRole("textbox", { name: "继续这次研究" }), { target: { value: "时间不限" } }); fireEvent.click(screen.getByRole("button", { name: "发送补充" }))
    const confirmation = await screen.findByRole("region", { name: "确认研究计划" })
    expect(confirmation).toHaveTextContent("时间从最近一年改为不限。")
    expect(vi.mocked(fetch).mock.calls.filter(call => String(call[0]).endsWith("search"))).toHaveLength(previousSearchCount)
    fireEvent.click(within(confirmation).getByRole("button", { name: "开始查找" }))
    await waitFor(() => expect(screen.getAllByRole("link", { name: "打开 GitHub" })).toHaveLength(2))
  })

  it("keeps an unsaved question retryable without starting a request when storage is full", async () => {
    render(<DesignDemoPage />)
    const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Storage full", "QuotaExceededError") })
    send("待保存的研究")
    expect(fetch).not.toHaveBeenCalled()
    expect(screen.queryByRole("button", { name: "停止" })).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "重试", exact: true })).toBeEnabled()
    expect(screen.getAllByText("待保存的研究").length).toBeGreaterThan(0)
    storage.mockRestore()
    fireEvent.click(screen.getByRole("button", { name: "重试", exact: true }))
    await screen.findByRole("link", { name: "打开 GitHub" })
    expect(readResearchWorkspace().visits).toHaveLength(1)
  })

  it("preserves Chinese composition, drafts and sidebar preferences", () => {
    const view = render(<DesignDemoPage />), input = screen.getByRole("textbox", { name: "向 Agora AI 提问" })
    fireEvent.change(input, { target: { value: "Agent" } }); fireEvent.compositionStart(input); fireEvent.submit(input.closest("form")!); expect(fetch).not.toHaveBeenCalled()
    fireEvent.compositionEnd(input); fireEvent.click(screen.getByRole("button", { name: "收起研究侧栏" })); view.unmount(); render(<DesignDemoPage />)
    expect(screen.getByRole("button", { name: "展开研究侧栏" })).toBeInTheDocument(); expect(screen.getByRole("textbox", { name: "向 Agora AI 提问" })).toHaveValue("Agent")
  })
})
