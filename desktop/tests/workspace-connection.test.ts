import { mkdtempSync, rmSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { connectionFailure, type ConnectionFailure } from "../src/load-failure"
import { isWorkspaceUrl } from "../src/navigation-policy"
import { connectionFilePath, ServerConnectionStore } from "../src/server-connection"
import { RETRY_DELAYS_MS, WorkspaceConnection, type WorkspaceSurface } from "../src/workspace-connection"

const CONNECTION_URL = "file:///C:/app/out/connect.html"

class FakeSurface implements WorkspaceSurface {
  url = ""
  workspaceLoads: string[] = []
  connectionPageLoads = 0
  /** Set to make the next workspace load reject with this Chromium description. */
  failNextLoad: string | null = null
  /** Where a successful workspace load ends up, e.g. after a server redirect. */
  landOn: ((requested: string) => string) | null = null
  beforeSettle: (() => void) | null = null

  async showConnectionPage() {
    if (this.url === CONNECTION_URL) return
    this.connectionPageLoads += 1
    this.url = CONNECTION_URL
  }

  async showWorkspace(url: string) {
    this.workspaceLoads.push(url)
    this.beforeSettle?.()
    if (this.failNextLoad) {
      const description = this.failNextLoad
      this.failNextLoad = null
      throw new Error(description)
    }
    this.url = this.landOn ? this.landOn(url) : url
  }

  currentUrl() {
    return this.url
  }
}

class FakeTimer {
  private time = 1_000
  private queue: Array<{ at: number; callback: () => void; handle: number }> = []
  private nextHandle = 1
  delays: number[] = []
  set(callback: () => void, delay: number) {
    this.delays.push(delay)
    const handle = this.nextHandle++
    this.queue.push({ at: this.time + delay, callback, handle })
    return handle
  }
  clear(handle: unknown) {
    this.queue = this.queue.filter(item => item.handle !== handle)
  }
  now() {
    return this.time
  }
  get pending() {
    return this.queue.length
  }
  async fire() {
    const item = this.queue.shift()
    if (!item) throw new Error("no retry scheduled")
    this.time = item.at
    item.callback()
    await flush()
  }
}

async function flush() {
  for (let index = 0; index < 10; index += 1) await Promise.resolve()
}

let directory: string
beforeEach(() => { directory = mkdtempSync(path.join(os.tmpdir(), "agora-workspace-")) })
afterEach(() => rmSync(directory, { recursive: true, force: true }))

function harness(options: { bundledOrigin?: string | null; probe?: (origin: string) => Promise<ConnectionFailure | null> } = {}) {
  const store = new ServerConnectionStore({
    connectionFile: connectionFilePath(directory),
    bundledOrigin: options.bundledOrigin === undefined ? "https://agora.example.com" : options.bundledOrigin,
    policy: { allowLoopbackHttp: false }
  })
  const surface = new FakeSurface()
  const timer = new FakeTimer()
  const probe = vi.fn(options.probe ?? (async () => null))
  const events: string[] = []
  const connection = new WorkspaceConnection({ store, surface, timer, probe, isWorkspaceUrl, onEvent: event => events.push(event) })
  const phases: string[] = []
  connection.onChange(status => phases.push(status.phase))
  return { connection, store, surface, timer, probe, events, phases }
}

describe("workspace connection", () => {
  it("asks for an address when the build has none", async () => {
    const { connection, surface, probe } = harness({ bundledOrigin: null })
    await expect(connection.connect()).resolves.toMatchObject({ phase: "unconfigured", origin: null })
    expect(surface.url).toBe(CONNECTION_URL)
    expect(probe).not.toHaveBeenCalled()
    expect(connection.workspaceOrigin).toBeNull()
  })

  it("probes the server, then shows the workspace", async () => {
    const { connection, surface, probe, phases } = harness()
    await expect(connection.connect()).resolves.toMatchObject({ phase: "connected", origin: "https://agora.example.com", source: "bundled" })
    expect(probe).toHaveBeenCalledWith("https://agora.example.com")
    expect(surface.workspaceLoads).toEqual(["https://agora.example.com/paper-agent"])
    expect(phases).toEqual(["connecting", "connected"])
    expect(surface.connectionPageLoads).toBe(1)
    expect(connection.workspaceOrigin).toBe("https://agora.example.com")
  })

  it("accepts the login page the server redirects a signed-out user to", async () => {
    const { connection, surface } = harness()
    surface.landOn = () => "https://agora.example.com/login?next=%2Fpaper-agent"
    await expect(connection.connect()).resolves.toMatchObject({ phase: "connected" })
  })

  it("shows why the probe failed and retries transient failures with growing delays", async () => {
    let failures = 2
    const { connection, surface, timer } = harness({ probe: async () => failures-- > 0 ? connectionFailure("refused") : null })
    await expect(connection.connect()).resolves.toMatchObject({ phase: "failed", failure: { code: "refused", retryable: true }, nextRetryAt: 1_000 + RETRY_DELAYS_MS[0] })
    expect(surface.url).toBe(CONNECTION_URL)
    expect(surface.workspaceLoads).toEqual([])
    await timer.fire()
    expect(connection.status.phase).toBe("failed")
    await timer.fire()
    expect(connection.status.phase).toBe("connected")
    expect(timer.delays).toEqual([RETRY_DELAYS_MS[0], RETRY_DELAYS_MS[1]])
  })

  it("waits for the user when only they or the administrator can fix the failure", async () => {
    const { connection, timer } = harness({ probe: async () => connectionFailure("certificate") })
    await expect(connection.connect()).resolves.toMatchObject({ phase: "failed", failure: { code: "certificate" }, nextRetryAt: null })
    expect(timer.pending).toBe(0)
  })

  it("reports a failed workspace load and a page that is not the workspace", async () => {
    const { connection, surface } = harness()
    surface.failNextLoad = "net::ERR_CONNECTION_RESET"
    await expect(connection.connect()).resolves.toMatchObject({ phase: "failed", failure: { code: "reset" } })
    expect(surface.url).toBe(CONNECTION_URL)
    surface.landOn = () => "https://agora.example.com/"
    await expect(connection.retry()).resolves.toMatchObject({ phase: "failed", failure: { code: "not_agora" } })
  })

  it("reports a refused cross-origin redirect instead of the aborted load", async () => {
    const { connection, surface } = harness()
    surface.beforeSettle = () => connection.redirectRefused()
    surface.failNextLoad = "net::ERR_ABORTED"
    await expect(connection.connect()).resolves.toMatchObject({ phase: "failed", failure: { code: "redirected", retryable: false } })
  })

  it("reports a server error answer for the workspace document", async () => {
    const { connection, surface } = harness()
    surface.beforeSettle = () => connection.mainFrameNavigated("https://agora.example.com/paper-agent", 502)
    await expect(connection.connect()).resolves.toMatchObject({ phase: "failed", failure: { code: "server_unavailable" } })
  })

  it("leaves a loaded workspace for the connection page when it later fails", async () => {
    const { connection, surface, timer } = harness()
    await connection.connect()
    connection.mainFrameNavigated("https://agora.example.com/paper-agent", 200)
    connection.mainFrameFailed("net::ERR_ABORTED")
    expect(connection.status.phase).toBe("connected")
    connection.mainFrameNavigated("https://agora.example.com/paper-agent", 503)
    await flush()
    expect(connection.status).toMatchObject({ phase: "failed", failure: { code: "server_unavailable" } })
    expect(surface.url).toBe(CONNECTION_URL)
    await timer.fire()
    expect(connection.status.phase).toBe("connected")
    connection.rendererGone()
    await flush()
    expect(connection.status).toMatchObject({ phase: "failed", failure: { code: "page_crashed" } })
  })

  it("verifies a new address before saving it and connects to it", async () => {
    const { connection, store, probe } = harness({ probe: async origin => origin === "https://wrong.example.com" ? connectionFailure("not_agora") : null })
    await connection.connect()
    await expect(connection.save("http://team.example.com")).resolves.toMatchObject({ ok: false, code: "server_url_insecure" })
    await expect(connection.save("wrong.example.com")).resolves.toMatchObject({ ok: false, code: "not_agora" })
    expect(store.view.source).toBe("bundled")
    await expect(connection.save("team.example.com")).resolves.toEqual({ ok: true })
    await flush()
    expect(connection.status).toMatchObject({ phase: "connected", origin: "https://team.example.com", source: "user" })
    expect(probe).toHaveBeenLastCalledWith("https://team.example.com")
    await expect(connection.reset()).resolves.toEqual({ ok: true })
    await flush()
    expect(connection.status).toMatchObject({ phase: "connected", origin: "https://agora.example.com", source: "bundled" })
  })

  it("holds the address form without retrying until the user saves or cancels", async () => {
    const { connection, surface, timer } = harness({ probe: async () => connectionFailure("refused") })
    await connection.connect()
    expect(timer.pending).toBe(1)
    await connection.edit()
    expect(connection.status).toMatchObject({ phase: "editing", nextRetryAt: null })
    expect(timer.pending).toBe(0)
    expect(connection.workspaceOrigin).toBeNull()
    await expect(connection.retry()).resolves.toMatchObject({ phase: "editing" })
    await expect(connection.cancelEdit()).resolves.toMatchObject({ phase: "failed" })
    expect(surface.workspaceLoads).toEqual([])
  })

  it("ignores the result of an attempt a newer one replaced", async () => {
    let release: (value: ConnectionFailure | null) => void = () => undefined
    const { connection, surface } = harness({ probe: () => new Promise(resolve => { release = resolve }) })
    const first = connection.connect()
    await flush()
    const editing = connection.edit()
    release(null)
    await first
    await editing
    expect(connection.status.phase).toBe("editing")
    expect(surface.workspaceLoads).toEqual([])
  })

  it("stops scheduling once disposed", async () => {
    const { connection, timer } = harness({ probe: async () => connectionFailure("timeout") })
    await connection.connect()
    connection.dispose()
    expect(timer.pending).toBe(0)
  })
})
