import { StrictMode } from "react"
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { DesignDemoPage } from "../design-demo-page"
import { recordResearchVisit, removeResearchVisit } from "@/lib/research/history"
import { researchQuestionHref } from "@/lib/research/entry"

const push = vi.hoisted(() => vi.fn())
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }))
vi.mock("@/components/auth/portal-account-control", () => ({ PortalAccountControl: () => <button>登录</button> }))

describe("research homepage", () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); push.mockReset() })
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it("starts automatically, fills examples without navigating, then sends once", () => {
    render(<DesignDemoPage />)
    expect(screen.getByRole("button", { name: "选择研究模式" })).toHaveTextContent("自动")
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled()
    expect(screen.queryByText("准备回答")).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "找可本地运行的开源项目" }))
    expect(screen.getByRole("textbox")).toHaveFocus()
    expect(push).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))
    expect(push).toHaveBeenCalledExactlyOnceWith(researchQuestionHref("projects", "找可本地运行的开源项目"))
    expect(screen.getByRole("status")).toHaveTextContent("正在打开项目雷达")
    expect(screen.queryByRole("link", { name: "进入模块" })).not.toBeInTheDocument()
  })

  it("asks a small clarifying choice without losing the question", () => {
    render(<DesignDemoPage />)
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "论文和开源项目" } })
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))
    expect(push).not.toHaveBeenCalled()
    expect(screen.getByRole("group", { name: "确认研究方向" })).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "找项目", exact: true }))
    expect(push).toHaveBeenCalledExactlyOnceWith(researchQuestionHref("projects", "论文和开源项目"))
  })

  it("does not send during Chinese composition and preserves input on failure", () => {
    render(<DesignDemoPage />)
    const input = screen.getByRole("textbox")
    fireEvent.change(input, { target: { value: "找研究基础设施项目" } })
    fireEvent.compositionStart(input)
    fireEvent.submit(input.closest("form")!)
    expect(push).not.toHaveBeenCalled()
    fireEvent.compositionEnd(input)
    push.mockImplementationOnce(() => { throw new Error("navigation unavailable") })
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }))
    expect(screen.getByRole("alert")).toHaveTextContent("问题已保留")
    expect(input).toHaveValue("找研究基础设施项目")
    expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled()
  })

  it("restores draft and manual mode after ordinary navigation in StrictMode", async () => {
    sessionStorage.setItem("agora-home-draft:v1", JSON.stringify({ query: "尚未发送的项目问题", mode: "projects" }))
    const view = render(<StrictMode><DesignDemoPage /></StrictMode>)
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("尚未发送的项目问题"))
    expect(screen.getByRole("button", { name: "选择研究模式" })).toHaveTextContent("找项目")
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "最新草稿" } })
    view.unmount()
    render(<StrictMode><DesignDemoPage /></StrictMode>)
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("最新草稿"))
  })

  it("only shows a real resume record and updates after clearing", () => {
    render(<DesignDemoPage />)
    expect(screen.queryByText("继续上次研究")).not.toBeInTheDocument()
    act(() => recordResearchVisit(researchQuestionHref("papers", "Agent") + "&sort=citations", 320))
    fireEvent.click(screen.getByRole("button", { name: /继续上次研究/ }))
    expect(push).toHaveBeenCalledWith(expect.stringContaining("sort=citations"), { scroll: false })
    act(() => removeResearchVisit())
    expect(screen.queryByText("继续上次研究")).not.toBeInTheDocument()
  })
})
