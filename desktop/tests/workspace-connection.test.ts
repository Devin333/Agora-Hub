import { mkdtempSync, rmSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { connectionFailure, type ConnectionFailure } from "../src/load-failure"
import { isWorkspaceUrl } from "../src/navigation-policy"
import { connectionFilePath, ServerConnectionStore } from "../src/server-connection"
import type { LocalImageRegistryId, LocalStackView } from "../src/contracts"
import type { LocalAdminInput } from "../src/local-stack-admin"
import type { LocalAdminResult, LocalStackResult } from "../src/local-stack"
import { parseChoice, RETRY_DELAYS_MS, WorkspaceConnection, type LocalStackPort, type WorkspaceSurface } from "../src/workspace-connection"

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

const LOCAL = "http://127.0.0.1:45180"

class FakeLocalStack implements LocalStackPort {
  result: LocalStackResult = { status: "ready", origin: LOCAL }
  adminResult: LocalAdminResult = { ok: true }
  starts = 0
  cancels = 0
  stops = 0
  registry: LocalImageRegistryId = "ghcr"
  admins: LocalAdminInput[] = []
  phase: LocalStackView["phase"] = "idle"
  pending: ((result: LocalStackResult) => void) | null = null
  private readonly listeners = new Set<(view: LocalStackView) => void>()

  get view(): LocalStackView {
    return { phase: this.phase, progress: null, output: [], failure: null, registry: this.registry, registries: [], imageTag: "v0.1.2", port: 45180 }
  }
  get busy() {
    return this.pending !== null
  }
  async ensureRunning() {
    this.starts += 1
    if (this.result.status === "cancelled") return await new Promise<LocalStackResult>(resolve => { this.pending = resolve })
    this.phase = this.result.status === "ready" ? "ready" : this.result.status === "needs-admin" ? "needs-admin" : "failed"
    return this.result
  }
  async createAdmin(input: LocalAdminInput) {
    this.admins.push(input)
    if (this.adminResult.ok) this.result = { status: "ready", origin: LOCAL }
    return this.adminResult
  }
  async setRegistry(registry: LocalImageRegistryId) {
    this.registry = registry
  }
  cancel() {
    this.cancels += 1
    this.pending?.({ status: "cancelled" })
    this.pending = null
  }
  async stop() {
    this.stops += 1
    this.phase = "stopped"
    return { ok: true }
  }
  onChange(listener: (view: LocalStackView) => void) {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }
  emit() {
    for (const listener of this.listeners) listener(this.view)
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

function harness(options: { officialOrigin?: string | null; probe?: (origin: string) => Promise<ConnectionFailure | null> } = {}) {
  const store = new ServerConnectionStore({
    connectionFile: connectionFilePath(directory),
    officialOrigin: options.officialOrigin === undefined ? "https://agora.example.com" : options.officialOrigin,
    policy: { allowLoopbackHttp: false }
  })
  const local = new FakeLocalStack()
  const surface = new FakeSurface()
  const timer = new FakeTimer()
  const probe = vi.fn(options.probe ?? (async () => null))
  const events: string[] = []
  const connection = new WorkspaceConnection({ store, local, surface, timer, probe, isWorkspaceUrl, onEvent: event => events.push(event) })
  const phases: string[] = []
  connection.onChange(status => phases.push(status.phase))
  return { connection, store, local, surface, timer, probe, events, phases }
}

describe("workspace connection", () => {
  it("asks for a connection when the build has no official service", async () => {
    const { connection, surface, probe } = harness({ officialOrigin: null })
    await expect(connection.connect()).resolves.toMatchObject({ phase: "unconfigured", mode: null, origin: null, officialOrigin: null })
    expect(surface.url).toBe(CONNECTION_URL)
    expect(probe).not.toHaveBeenCalled()
    expect(connection.workspaceOrigin).toBeNull()
  })

  it("probes the server, then shows the workspace", async () => {
    const { connection, surface, probe, phases } = harness()
    await expect(connection.connect()).resolves.toMatchObject({ phase: "connected", mode: "official", origin: "https://agora.example.com" })
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

  it("verifies a new server address before saving it and connects to it", async () => {
    const { connection, store, probe } = harness({ probe: async origin => origin === "https://wrong.example.com" ? connectionFailure("not_agora") : null })
    await connection.connect()
    await expect(connection.choose({ mode: "server", address: "team.example.com" })).resolves.toMatchObject({ ok: false, code: "not_choosing" })
    await connection.edit()
    await expect(connection.choose({ mode: "server", address: "http://team.example.com" })).resolves.toMatchObject({ ok: false, code: "server_url_insecure" })
    await expect(connection.choose({ mode: "server", address: "wrong.example.com" })).resolves.toMatchObject({ ok: false, code: "not_agora" })
    expect(store.view.mode).toBe("official")
    await expect(connection.choose({ mode: "server", address: "team.example.com" })).resolves.toEqual({ ok: true })
    await flush()
    expect(connection.status).toMatchObject({ phase: "connected", mode: "server", origin: "https://team.example.com", serverOrigin: "https://team.example.com" })
    expect(probe).toHaveBeenLastCalledWith("https://team.example.com")
    await connection.edit()
    await expect(connection.choose({ mode: "official" })).resolves.toEqual({ ok: true })
    await flush()
    expect(connection.status).toMatchObject({ phase: "connected", mode: "official", origin: "https://agora.example.com", serverOrigin: "https://team.example.com" })
  })

  it("refuses the official choice in a build without one and malformed choices", async () => {
    const { connection } = harness({ officialOrigin: null })
    await connection.connect()
    await expect(connection.choose({ mode: "official" })).resolves.toMatchObject({ ok: false, code: "official_unavailable" })
    await expect(connection.choose({ mode: "cloud" })).resolves.toMatchObject({ ok: false, code: "choice_invalid" })
    await expect(connection.choose("local")).resolves.toMatchObject({ ok: false, code: "choice_invalid" })
  })

  it("starts the local stack before saving local mode, then opens it", async () => {
    const { connection, store, local, probe } = harness({ officialOrigin: null })
    await connection.connect()
    await expect(connection.choose({ mode: "local", registry: "acr" })).resolves.toEqual({ ok: true })
    expect(local.registry).toBe("acr")
    await flush()
    expect(store.view.mode).toBe("local")
    expect(connection.status).toMatchObject({ phase: "connected", mode: "local", origin: LOCAL })
    expect(connection.workspaceOrigin).toBe(LOCAL)
    expect(probe).toHaveBeenLastCalledWith(LOCAL)
  })

  it("keeps the previous choice when the local stack cannot start", async () => {
    const { connection, store, local } = harness()
    await connection.connect()
    await connection.edit()
    local.result = { status: "failed", failure: connectionFailure("docker_missing") }
    await expect(connection.choose({ mode: "local" })).resolves.toMatchObject({ ok: false, code: "docker_missing" })
    expect(store.view.mode).toBe("official")
    expect(connection.status.phase).toBe("editing")
  })

  it("asks for the first administrator, creates it and only then saves local mode", async () => {
    const { connection, store, local } = harness({ officialOrigin: null })
    await connection.connect()
    local.result = { status: "needs-admin", origin: LOCAL }
    await expect(connection.choose({ mode: "local" })).resolves.toMatchObject({ ok: false, code: "local_admin_required" })
    expect(store.view.mode).toBeNull()
    await expect(connection.createLocalAdmin({ username: "owner", password: "short", confirmation: "short" })).resolves.toMatchObject({ ok: false, code: "password_too_short" })
    expect(local.admins).toEqual([])
    await expect(connection.createLocalAdmin({ username: "owner", password: "local-pass-1", confirmation: "local-pass-1" })).resolves.toEqual({ ok: true })
    expect(local.admins).toEqual([{ username: "owner", password: "local-pass-1" }])
    await flush()
    expect(store.view.mode).toBe("local")
    expect(connection.status).toMatchObject({ phase: "connected", origin: LOCAL })
  })

  it("shows the administrator form when a saved local stack has no account yet", async () => {
    const { connection, store, local } = harness({ officialOrigin: null })
    await store.saveLocal()
    local.result = { status: "needs-admin", origin: LOCAL }
    await expect(connection.connect()).resolves.toMatchObject({ phase: "local-setup", mode: "local" })
    expect(connection.workspaceOrigin).toBeNull()
    await expect(connection.retry()).resolves.toMatchObject({ phase: "local-setup" })
  })

  it("reports a local stack failure on the connection page and retries the transient ones", async () => {
    const { connection, store, local, timer } = harness({ officialOrigin: null })
    await store.saveLocal()
    local.result = { status: "failed", failure: connectionFailure("docker_not_running") }
    await expect(connection.connect()).resolves.toMatchObject({ phase: "failed", failure: { code: "docker_not_running", retryable: true } })
    local.result = { status: "ready", origin: LOCAL }
    await timer.fire()
    expect(connection.status).toMatchObject({ phase: "connected", origin: LOCAL })
  })

  it("interrupts a local start when the user switches away and stops the stack on request", async () => {
    const { connection, store, local, surface } = harness({ officialOrigin: null })
    await store.saveLocal()
    local.result = { status: "cancelled" }
    const attempt = connection.connect()
    await flush()
    expect(connection.status.phase).toBe("connecting")
    await connection.edit()
    await attempt
    expect(local.cancels).toBe(1)
    expect(connection.status.phase).toBe("editing")
    local.result = { status: "ready", origin: LOCAL }
    await connection.cancelEdit()
    expect(connection.status.phase).toBe("connected")
    await connection.stopLocal()
    expect(local.stops).toBe(1)
    expect(connection.status).toMatchObject({ phase: "failed", failure: { code: "local_stack_stopped" }, nextRetryAt: null })
    expect(surface.url).toBe(CONNECTION_URL)
  })

  it("passes the progress of the local stack on to the page", async () => {
    const { connection, local } = harness()
    const seen: string[] = []
    connection.onChange(status => seen.push(status.local.phase))
    local.phase = "pulling"
    local.emit()
    expect(seen).toEqual(["pulling"])
  })

  it("accepts only the choices the connection page can send", () => {
    expect(parseChoice({ mode: "official", address: "x" })).toEqual({ mode: "official" })
    expect(parseChoice({ mode: "server", address: "a" })).toEqual({ mode: "server", address: "a" })
    expect(parseChoice({ mode: "server" })).toBeNull()
    expect(parseChoice({ mode: "local" })).toEqual({ mode: "local" })
    expect(parseChoice({ mode: "local", registry: "acr" })).toEqual({ mode: "local", registry: "acr" })
    expect(parseChoice({ mode: "local", registry: "docker.io" })).toBeNull()
    expect(parseChoice(["local"])).toBeNull()
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
