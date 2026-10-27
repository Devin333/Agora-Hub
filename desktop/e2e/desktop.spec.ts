import { spawn } from "node:child_process"
import { mkdtemp, readFile, rm } from "node:fs/promises"
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http"
import type { AddressInfo } from "node:net"
import { createRequire } from "node:module"
import os from "node:os"
import path from "node:path"
import { expect, test, _electron as electron, type ElectronApplication, type Page } from "@playwright/test"

const desktopRoot = path.resolve(__dirname, "..")
const environment = { ...process.env, AGORA_DESKTOP_TEST_MODE: "1" }

/** A loopback stand-in for the hosted web client: its health route and the workspace pages the client may show. */
class FakeAgoraServer {
  workspaceStatus = 200
  readonly requests: string[] = []
  private server: Server | null = null

  get origin(): string {
    const address = this.server?.address() as AddressInfo | null
    if (!address) throw new Error("fake server is not listening")
    return `http://127.0.0.1:${address.port}`
  }

  async start(): Promise<void> {
    this.server = createServer((request, response) => this.handle(request, response))
    await new Promise<void>(resolve => this.server?.listen(0, "127.0.0.1", resolve))
  }

  async stop(): Promise<void> {
    const server = this.server
    this.server = null
    if (!server) return
    server.closeAllConnections()
    await new Promise<void>(resolve => server.close(() => resolve()))
  }

  private handle(request: IncomingMessage, response: ServerResponse): void {
    const url = new URL(request.url ?? "/", "http://127.0.0.1")
    this.requests.push(url.pathname)
    if (url.pathname === "/api/health") {
      response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" })
      response.end(JSON.stringify({ success: true, data: { status: "ok", service: "agora-hub-frontend", deploymentProfile: "hosted", ready: true } }))
      return
    }
    if (url.pathname === "/paper-agent") {
      response.writeHead(this.workspaceStatus, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" })
      response.end(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>论文工作台</title></head><body><h1 id="workspace">论文工作台</h1><a id="outside" href="${this.origin}/admin">管理</a></body></html>`)
      return
    }
    response.writeHead(404, { "Content-Type": "text/plain" })
    response.end("not found")
  }
}

async function launch(userData: string): Promise<{ app: ElectronApplication; page: Page }> {
  const app = await electron.launch({ args: [desktopRoot, `--agora-test-user-data-dir=${userData}`], env: environment, timeout: 120_000 })
  return { app, page: await app.firstWindow() }
}

async function quit(app: ElectronApplication): Promise<void> {
  const child = app.process()
  if (child.exitCode !== null) return
  const exited = new Promise<void>(resolve => child.once("exit", () => resolve()))
  await app.evaluate(({ app: electronApp }) => { setTimeout(() => electronApp.exit(0), 0) }).catch(() => undefined)
  await Promise.race([exited, new Promise<void>(resolve => setTimeout(resolve, 10_000))])
  if (child.exitCode === null) child.kill()
}

function runSecondInstance(userData: string): Promise<number | null> {
  const require = createRequire(path.join(desktopRoot, "package.json"))
  const electronExecutable = require("electron") as string
  return new Promise((resolve, reject) => {
    const child = spawn(electronExecutable, [desktopRoot, `--agora-test-user-data-dir=${userData}`], { windowsHide: true, stdio: "ignore", env: environment })
    child.once("error", reject)
    child.once("exit", resolve)
  })
}

const bridgeShape = (page: Page) => page.evaluate(() => {
  const host = globalThis as unknown as { agoraDesktop?: Record<string, object>; agoraConnection?: object; process?: unknown; require?: unknown }
  return {
    desktop: host.agoraDesktop ? Object.keys(host.agoraDesktop).sort() : null,
    lifecycle: host.agoraDesktop ? Object.keys(host.agoraDesktop.lifecycle).sort() : null,
    connection: host.agoraConnection ? Object.keys(host.agoraConnection).sort() : null,
    nodeProcess: typeof host.process,
    nodeRequire: typeof host.require
  }
})

test.describe("Agora Paper Agent desktop client", () => {
  let userData: string
  let server: FakeAgoraServer

  test.beforeEach(async () => {
    userData = await mkdtemp(path.join(os.tmpdir(), "agora-electron-e2e-"))
    server = new FakeAgoraServer()
    await server.start()
  })

  test.afterEach(async () => {
    await server.stop()
    await rm(userData, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
  })

  test("asks for a server, verifies it, opens the workspace and reconnects there on the next launch", async () => {
    const { app, page } = await launch(userData)
    try {
      await expect.poll(() => page.url(), { timeout: 60_000 }).toMatch(/^file:.*\/connect\.html$/i)
      await expect(page.locator("#title")).toHaveText("连接到 Agora 服务器")
      expect(await bridgeShape(page)).toEqual({
        desktop: null,
        lifecycle: null,
        connection: ["cancelEdit", "edit", "onChange", "reset", "retry", "save", "status", "version"],
        nodeProcess: "undefined",
        nodeRequire: "undefined"
      })

      const sizing = await app.evaluate(({ BrowserWindow, screen }) => {
        const window = BrowserWindow.getAllWindows()[0]
        const bounds = window.getBounds()
        return { bounds, minimum: window.getMinimumSize(), workArea: screen.getDisplayMatching(bounds).workArea }
      })
      expect(sizing.minimum[0]).toBeLessThanOrEqual(sizing.workArea.width)
      expect(sizing.bounds.width).toBeLessThanOrEqual(sizing.workArea.width)
      expect(sizing.bounds.height).toBeLessThanOrEqual(sizing.workArea.height)

      // A fresh profile follows the OS; the frame and the connection page paint the matching background.
      const native = await app.evaluate(({ BrowserWindow, nativeTheme }) => ({
        themeSource: nativeTheme.themeSource,
        dark: nativeTheme.shouldUseDarkColors,
        background: BrowserWindow.getAllWindows()[0].getBackgroundColor().toLowerCase()
      }))
      expect(native.themeSource).toBe("system")
      expect(native.background).toBe(native.dark ? "#161616" : "#ffffff")
      await page.emulateMedia({ colorScheme: null })
      expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe(native.dark ? "rgb(22, 22, 22)" : "rgb(255, 255, 255)")

      await page.locator("#address").fill("http://agora.example.com")
      await page.locator("#save").click()
      await expect(page.locator("#address-error")).toHaveText("服务器必须使用 HTTPS。请让管理员为服务器配置域名和证书。")

      const closedPort = await (async () => {
        const probe = new FakeAgoraServer()
        await probe.start()
        const origin = probe.origin
        await probe.stop()
        return origin
      })()
      await page.locator("#address").fill(closedPort)
      await page.locator("#save").click()
      await expect(page.locator("#address-error")).toHaveText("服务器拒绝了连接，服务可能尚未启动。请稍后重试或联系管理员。")
      expect(page.url()).toMatch(/connect\.html$/)

      await page.locator("#address").fill(server.origin)
      await page.locator("#save").click()
      await expect.poll(() => page.url(), { timeout: 30_000 }).toBe(`${server.origin}/paper-agent`)
      await expect(page.locator("#workspace")).toHaveText("论文工作台")
      expect(await bridgeShape(page)).toEqual({
        desktop: ["appearance", "export", "lifecycle", "version"],
        lifecycle: ["onQuitRequested", "respondToQuit", "updateState"],
        connection: null,
        nodeProcess: "undefined",
        nodeRequire: "undefined"
      })
      await expect(page.evaluate(() => (globalThis as unknown as { agoraDesktop: { appearance: { set(value: string): Promise<{ preference: string }> } } }).agoraDesktop.appearance.set("system")))
        .resolves.toMatchObject({ preference: "system" })
      const saved = JSON.parse(await readFile(path.join(userData, "host-preferences", "connection.json"), "utf8"))
      expect(saved).toEqual({ schema: "agora.desktop-connection/v1", origin: server.origin })

      // Other pages of the service and new windows never open inside the client.
      const windowsBefore = app.windows().length
      expect(await page.evaluate(() => (globalThis as unknown as { open(url: string): unknown }).open("file:///C:/Windows/win.ini"))).toBeNull()
      // The host cancels the navigation before it starts, so the click is dispatched in the page rather than awaited as one.
      await page.evaluate(() => document.getElementById("outside")?.click())
      await expect.poll(() => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.isLoading())).toBe(false)
      expect(app.windows().length).toBe(windowsBefore)
      expect(page.url()).toBe(`${server.origin}/paper-agent`)
      expect(server.requests).not.toContain("/admin")
    } finally {
      await quit(app)
    }

    const relaunched = await launch(userData)
    try {
      await expect.poll(() => relaunched.page.url(), { timeout: 60_000 }).toBe(`${server.origin}/paper-agent`)
    } finally {
      await quit(relaunched.app)
    }
  })

  test("shows why the server is unavailable and returns to the workspace once it recovers", async () => {
    const first = await launch(userData)
    try {
      await expect.poll(() => first.page.url(), { timeout: 60_000 }).toMatch(/connect\.html$/i)
      await first.page.locator("#address").fill(server.origin)
      await first.page.locator("#save").click()
      await expect.poll(() => first.page.url(), { timeout: 30_000 }).toBe(`${server.origin}/paper-agent`)

      server.workspaceStatus = 503
      await first.page.reload()
      await expect.poll(() => first.page.url(), { timeout: 30_000 }).toMatch(/connect\.html$/i)
      await expect(first.page.locator("#title")).toHaveText("无法连接服务器")
      await expect(first.page.locator("#message")).toHaveText("服务器暂时不可用，可能正在升级或维护。稍后会自动重新连接。")
      await expect(first.page.locator("#status")).toContainText("秒后自动重试")
      const tray = await first.app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length)
      expect(tray).toBe(1)

      server.workspaceStatus = 200
      await first.page.locator("#retry").click()
      await expect.poll(() => first.page.url(), { timeout: 30_000 }).toBe(`${server.origin}/paper-agent`)
    } finally {
      await quit(first.app)
    }
  })

  test("keeps one resident instance that hides on close and comes back on a second launch", async () => {
    const { app, page } = await launch(userData)
    try {
      await expect.poll(() => page.url(), { timeout: 60_000 }).toMatch(/connect\.html$/i)
      // The duplicate exits once it has handed its launch over; the resident instance handles the hand-over afterwards.
      await app.evaluate(({ app: electronApp }) => {
        const host = globalThis as unknown as { secondInstances: number }
        host.secondInstances = 0
        electronApp.on("second-instance", () => { host.secondInstances += 1 })
      })
      const handedOver = () => app.evaluate(() => (globalThis as unknown as { secondInstances: number }).secondInstances)
      expect(await runSecondInstance(userData)).toBe(0)
      await expect.poll(handedOver).toBe(1)
      expect(app.windows().length).toBe(1)

      const windowState = () => app.evaluate(({ BrowserWindow }) => {
        const windows = BrowserWindow.getAllWindows()
        return { count: windows.length, visible: windows[0]?.isVisible() ?? false }
      })
      await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close())
      await expect.poll(windowState).toEqual({ count: 1, visible: false })
      expect(app.process().exitCode).toBeNull()
      expect(await runSecondInstance(userData)).toBe(0)
      await expect.poll(handedOver).toBe(2)
      await expect.poll(windowState).toEqual({ count: 1, visible: true })
    } finally {
      await quit(app)
    }
  })
})
