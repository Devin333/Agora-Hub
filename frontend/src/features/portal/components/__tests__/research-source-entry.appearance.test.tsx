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
  const view = render(<ResearchSourceEntry appearance={appearance} />)
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

  // Launches a real Chromium to resolve the cascade, so it needs a browser-scale budget.
  it("resolves status and error text to the global theme tokens for both appearances in light and dark", { timeout: 60_000 }, async () => {
    const cases: string[] = []
    const componentClasses: Record<string, string> = {}
    for (const appearance of ["portal", "paper-agent"] as const) {
      for (const [name, record] of [
        ["failed", { ...identity, status: "failed", error: { code: "research_parse_unavailable", message: "Research operation failed (research_parse_unavailable)." } }],
        ["running", { ...identity, status: "running" }],
        ["completion-missing-paper", { ...identity, status: "passed" }],
        ["completed", { ...identity, status: "passed", paperId: "paper-fixture" }],
      ] as const) {
        cleanup()
        const rendered = await renderRecord(record, appearance)
        componentClasses.status = rendered.entry.querySelector<HTMLElement>('[data-slot="status"]')!.classList[0]
        cases.push(`<section data-case="${appearance}:${name}" data-host="${appearance}">${rendered.html}</section>`)
      }
      cleanup()
      sessionStorage.setItem(storageKey, "{")
      const errorView = render(<ResearchSourceEntry appearance={appearance} />)
      const alert = await screen.findByRole("alert")
      componentClasses.error = alert.classList[0]
      cases.push(`<section data-case="${appearance}:error" data-host="${appearance}">${errorView.container.innerHTML}</section>`)
    }

    const componentCss = scopedCss("src/features/portal/components/research-source-entry.module.css", componentClasses)
    const appCss = scopedCss("src/features/paper-agent-app/paper-agent-app.module.css", { onlineSearch: paperStyles.onlineSearch })
    const { chromium } = await import("playwright")
    const browser = await chromium.launch({ headless: true })
    try {
      const page = await browser.newPage()
      // The portal renders the entry straight on the page; the paper agent renders it inside its online search panel.
      const body = cases.map(html => html.includes('data-host="paper-agent"') ? `<div class="${paperStyles.onlineSearch}">${html}</div>` : html).join("")
      const tokenCss = readFileSync(path.join(process.cwd(), "src/styles/theme-tokens.css"), "utf8")
      const seen: Record<string, { status: string; error: string }> = {}
      for (const theme of ["light", "dark"] as const) {
        await page.setContent(`<!doctype html><html data-theme="${theme}"><head><style>${tokenCss}
${componentCss}
${appCss}</style></head><body><span data-probe="status" style="color: var(--muted-foreground)"></span><span data-probe="error" style="color: var(--destructive-text)"></span><span data-probe="foreground" style="color: var(--foreground)"></span>${body}</body></html>`)
        const probe = await page.$$eval("[data-probe]", nodes => Object.fromEntries(nodes.map(node => [node.getAttribute("data-probe"), getComputedStyle(node).color])))
        expect(probe.status, `${theme} muted-foreground`).not.toBe(probe.foreground)
        expect(probe.error, `${theme} destructive-text`).not.toBe(probe.foreground)
        seen[theme] = { status: probe.status, error: probe.error }
        const results = await page.$$eval("[data-case]", nodes => nodes.map(node => ({
          name: node.getAttribute("data-case")!,
          status: Array.from(node.querySelectorAll<HTMLElement>('[data-slot="status"], [data-slot="status"] span, [data-slot="status"] button, [data-slot="status"] a')).map(element => getComputedStyle(element).color),
          error: Array.from(node.querySelectorAll<HTMLElement>('[data-slot="error"]')).map(element => getComputedStyle(element).color),
        })))
        expect(results).toHaveLength(10)
        for (const result of results) {
          const label = `${theme} ${result.name}`
          if (result.name.endsWith(":error")) {
            expect(result.error, label).toEqual([probe.error])
          } else {
            expect(result.status, label).not.toHaveLength(0)
            expect(new Set(result.status), label).toEqual(new Set([probe.status]))
          }
        }
      }
      // The two themes must actually paint different inks, otherwise the cascade is not following data-theme.
      expect(seen.light.status).not.toBe(seen.dark.status)
      expect(seen.light.error).not.toBe(seen.dark.error)
    } finally {
      await browser.close()
    }
  })

  it("lets a host recolor the entry through the status and error hooks", async () => {
    const failed = await renderRecord({ ...identity, status: "failed" }, "paper-agent")
    const statusClass = failed.entry.querySelector<HTMLElement>('[data-slot="status"]')!.classList[0]
    cleanup()
    sessionStorage.setItem(storageKey, "{")
    const errorView = render(<ResearchSourceEntry appearance="paper-agent" />)
    const alert = await screen.findByRole("alert")
    const componentCss = scopedCss("src/features/portal/components/research-source-entry.module.css", { status: statusClass, error: alert.classList[0] })
    const tokenCss = readFileSync(path.join(process.cwd(), "src/styles/theme-tokens.css"), "utf8")
    const { chromium } = await import("playwright")
    const browser = await chromium.launch({ headless: true })
    try {
      const page = await browser.newPage()
      await page.setContent(`<!doctype html><html data-theme="light"><head><style>${tokenCss}
${componentCss}</style></head><body><section data-case="status" style="--research-source-status-color: rgb(33, 33, 31)">${failed.html}</section><section data-case="error" style="--research-source-error-color: rgb(12, 34, 56)">${errorView.container.innerHTML}</section></body></html>`)
      expect(await page.$eval('[data-case="status"] [data-slot="status"]', element => getComputedStyle(element).color)).toBe("rgb(33, 33, 31)")
      expect(await page.$eval('[data-case="status"] [data-slot="status"] button', element => getComputedStyle(element).color)).toBe("rgb(33, 33, 31)")
      expect(await page.$eval('[data-case="error"] [data-slot="error"]', element => getComputedStyle(element).color)).toBe("rgb(12, 34, 56)")
    } finally {
      await browser.close()
    }
  })
})
