import { cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import { readFileSync } from "node:fs"
import path from "node:path"
import paperStyles from "@/features/paper-agent-app/paper-agent-app.module.css"
import { acceptAccountHistory, selectHistoryOwner } from "@/lib/research/history"
import { emptyWorkspace } from "@/lib/research/history-model"
import { ResearchSourceEntry } from "../research-source-entry"

const owner = "alice"
const storageKey = `agora-pdf-import:${owner}`
const identity = {
  importId: "imp_fixture",
  requestId: "request-fixture-1",
  generation: `gen_${"1".repeat(32)}`,
  fingerprint: `sha256:${"2".repeat(64)}`,
  filename: "真实论文.pdf",
}

function scopedCss(filename: string, classes: Record<string, string>) {
  let css = readFileSync(path.join(process.cwd(), filename), "utf8")
  for (const [local, scoped] of Object.entries(classes).sort(([left], [right]) => right.length - left.length)) {
    css = css.replace(new RegExp(`\\.${local}(?![\\w-])`, "g"), `.${scoped}`)
  }
  return css
}

async function renderRecord(record: object | null, appearance: "portal" | "paper-agent" = "portal") {
  sessionStorage.clear()
  if (record) sessionStorage.setItem(storageKey, JSON.stringify(record))
  const view = render(
    <div className={paperStyles.onlineSearch}>
      <ResearchSourceEntry appearance={appearance} />
    </div>,
  )
  const entry = await waitFor(() => view.container.querySelector<HTMLElement>("[data-research-source-entry]")!)
  return { html: view.container.innerHTML, entry }
}

beforeEach(() => {
  sessionStorage.clear()
  selectHistoryOwner(owner)
  acceptAccountHistory(emptyWorkspace(), 0)
})

afterEach(() => {
  cleanup()
  selectHistoryOwner(null)
})

describe("ResearchSourceEntry paper-agent appearance", () => {
  it("renders production status DOM for failure, processing, completion-without-paper, completion, and missing states", async () => {
    // These receipt fixtures exercise the production component's presentation contract only; they are not evidence of a real runtime result.
    let rendered = await renderRecord({
      ...identity,
      status: "failed",
      error: { message: "Research operation failed (research_parse_unavailable)." },
    })
    expect(rendered.entry).toHaveAttribute("data-state", "failed")
    expect(screen.getByText("论文解析运行时尚未配置（research_parse_unavailable），请配置后重新选择文件创建新请求")).toBeVisible()
    expect(screen.getByRole("button", { name: "重新选择文件并创建新请求" })).toBeVisible()

    cleanup()
    rendered = await renderRecord({ ...identity, status: "running" })
    expect(rendered.entry).toHaveAttribute("data-state", "running")
    expect(screen.getByText("PDF 正在处理中，等待真实转换结果")).toBeVisible()

    cleanup()
    rendered = await renderRecord({ ...identity, status: "passed" })
    expect(rendered.entry).toHaveAttribute("data-state", "passed")
    expect(screen.getByText("导入回执缺少论文标识，尚不能打开阅读器或用于研究，请检查已有请求")).toBeVisible()

    cleanup()
    rendered = await renderRecord({ ...identity, status: "passed", paperId: "paper-fixture" })
    expect(rendered.entry).toHaveAttribute("data-state", "completed")
    expect(screen.getByText("文本已转换，可用于本次研究")).toBeVisible()

    cleanup()
    rendered = await renderRecord(null)
    expect(rendered.entry).toHaveAttribute("data-state", "missing")
    expect(rendered.entry).toHaveClass("sr-only")
  })

  it("uses the real component DOM and CSS cascade for monochrome computed colors in both themes", async () => {
    const cases: string[] = []
    const componentClasses: Record<string, string> = {}
    for (const [name, record] of [
      ["failed", { ...identity, status: "failed", error: { code: "research_parse_unavailable", message: "Research operation failed (research_parse_unavailable)." } }],
      ["running", { ...identity, status: "running" }],
      ["completion-missing-paper", { ...identity, status: "passed" }],
      ["completed", { ...identity, status: "passed", paperId: "paper-fixture" }],
    ] as const) {
      cleanup()
      const rendered = await renderRecord(record)
      componentClasses.root = rendered.entry.classList[0]
      componentClasses.status = rendered.entry.querySelector<HTMLElement>('[data-slot="status"]')!.classList[0]
      cases.push(`<section data-case="${name}">${rendered.html}</section>`)
    }
    cleanup()
    sessionStorage.setItem(storageKey, "{")
    const errorView = render(<div className={paperStyles.onlineSearch}><ResearchSourceEntry /></div>)
    const alert = await screen.findByRole("alert")
    componentClasses.error = alert.classList[0]
    cases.push(`<section data-case="error">${errorView.container.innerHTML}</section>`)

    const componentCss = scopedCss("src/features/portal/components/research-source-entry.module.css", componentClasses)
    const appCss = scopedCss("src/features/paper-agent-app/paper-agent-app.module.css", {
      app: paperStyles.app,
      onlineSearch: paperStyles.onlineSearch,
    })
    const themeCss = readFileSync(path.join(process.cwd(), "src/features/paper-agent-app/paper-agent-theme.css"), "utf8")
    const { chromium } = await import("playwright")
    const browser = await chromium.launch({ headless: true })
    try {
      const page = await browser.newPage()
      for (const [theme, muted] of [["light", "rgb(99, 99, 99)"], ["dark", "rgb(166, 166, 166)"]] as const) {
        await page.setContent(`<style>${themeCss}\n${componentCss}\n${appCss}</style><main class="${paperStyles.app}" data-paper-agent-shell data-theme="${theme}">${cases.join("")}</main>`)
        const colors = await page.$$eval("[data-case]", nodes => nodes.map(node => ({
          name: node.getAttribute("data-case"),
          colors: Array.from(node.querySelectorAll<HTMLElement>('[data-slot], [data-slot] span, [data-slot] button')).map(element => getComputedStyle(element).color),
        })))
        for (const result of colors) {
          expect(result.colors, result.name ?? "unknown state").not.toHaveLength(0)
          expect(new Set(result.colors), result.name ?? "unknown state").toEqual(new Set([muted]))
        }
      }
    } finally {
      await browser.close()
    }
  })

  it("preserves portal defaults while explicit paper-agent appearance inherits its host color", async () => {
    const portal = await renderRecord({ ...identity, status: "failed" })
    const componentCss = scopedCss("src/features/portal/components/research-source-entry.module.css", {
      root: portal.entry.classList[0],
      status: portal.entry.querySelector<HTMLElement>('[data-slot="status"]')!.classList[0],
    })
    cleanup()
    const monochrome = await renderRecord({ ...identity, status: "failed" }, "paper-agent")
    const { chromium } = await import("playwright")
    const browser = await chromium.launch({ headless: true })
    try {
      const page = await browser.newPage()
      await page.setContent(`<style>${componentCss}</style><section data-case="portal">${portal.html}</section><section data-case="paper" style="color: rgb(33, 33, 31)">${monochrome.html}</section>`)
      expect(await page.$eval('[data-case="portal"] [data-slot="status"]', element => getComputedStyle(element).color)).toBe("rgb(109, 40, 217)")
      expect(await page.$eval('[data-case="paper"] [data-slot="status"]', element => getComputedStyle(element).color)).toBe("rgb(33, 33, 31)")
    } finally {
      await browser.close()
    }
  })
})


