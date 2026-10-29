import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import type { DockerCommandOptions, DockerCommandResult, DockerRunner } from "../src/docker-cli"
import { parseEnvFile } from "../src/local-stack-env"
import { DEFAULT_LOCAL_PORT, LOCAL_STACK_PROJECT, LocalStackController, PullProgress } from "../src/local-stack"

const RESOURCES = path.resolve(__dirname, "../../deploy/desktop")
const TAG = "v0.1.2"

type Handler = (args: readonly string[], options: DockerCommandOptions) => Partial<DockerCommandResult> | Promise<Partial<DockerCommandResult>>

/** Records every Docker command and answers it like a healthy Docker Desktop with nothing pulled yet. */
class FakeDocker {
  readonly calls: Array<{ args: readonly string[]; input?: string }> = []
  readonly overrides = new Map<string, Handler>()
  accounts = 0
  imagesPresent = false

  readonly run: DockerRunner = async (args, options) => {
    this.calls.push({ args, input: options.input })
    const key = FakeDocker.key(args)
    const handler = this.overrides.get(key) ?? ((callArgs, callOptions) => this.default(key, callArgs, callOptions))
    const result = await handler(args, options)
    return { code: 0, stdout: "", stderr: "", timedOut: false, aborted: false, ...result }
  }

  /** The subcommand, skipping compose's own global flags. */
  static key(args: readonly string[]): string {
    if (args[0] !== "compose") return args.slice(0, 2).join(" ")
    const rest = [...args.slice(1)]
    while (rest[0]?.startsWith("--")) rest.splice(0, 2)
    // exec -T api news accounts <command>
    return `compose ${rest[0]}${rest[0] === "exec" ? ` ${rest[4]} ${rest[5]}` : ""}`
  }

  commands(): string[] {
    return this.calls.map(call => FakeDocker.key(call.args))
  }

  private default(key: string, _args: readonly string[], options: DockerCommandOptions): Partial<DockerCommandResult> {
    switch (key) {
      case "version --format": return { stdout: "29.4.2\n" }
      case "compose version": return { stdout: "5.1.3\n" }
      case "compose ps": return { stdout: "" }
      case "image inspect": return this.imagesPresent ? { stdout: "sha256:1\n" } : { code: 1, stderr: "Error: No such image" }
      case "compose pull":
        for (const line of [
          '{"id":"Image a","status":"Working","text":"Pulling"}',
          '{"id":"l1","parent_id":"Image a","status":"Working","text":"Downloading","current":50,"total":100}',
          '{"id":"l1","parent_id":"Image a","status":"Done","text":"Pull complete"}'
        ]) options.onLine?.(line)
        this.imagesPresent = true
        return {}
      case "compose up": return {}
      case "compose exec accounts list": return { stdout: JSON.stringify({ accounts: Array.from({ length: this.accounts }, () => ({})) }) }
      case "compose exec accounts create-admin":
        this.accounts += 1
        return { stdout: '{"account": {}}' }
      default: return {}
    }
  }
}

let directory: string
beforeEach(() => { directory = mkdtempSync(path.join(os.tmpdir(), "agora-stack-")) })
afterEach(() => rmSync(directory, { recursive: true, force: true }))

interface HarnessOptions {
  docker?: FakeDocker | null
  reports?: Array<string | null>
  free?(port: number): boolean
  registryOverride?: string | null
}

function harness(overrides: HarnessOptions = {}) {
  const docker = overrides.docker === undefined ? new FakeDocker() : overrides.docker
  const reports = overrides.reports ?? [TAG]
  const probes: Array<{ origin: string; token: string }> = []
  const events: string[] = []
  let nextFree = 50_000
  const controller = new LocalStackController({
    directory,
    resourceDirectory: RESOURCES,
    imageTag: TAG,
    docker: () => docker?.run ?? null,
    probeIdentity: async (origin, token) => {
      probes.push({ origin, token })
      return reports.length > 1 ? reports.shift()! : reports[0]
    },
    ports: { isFree: async port => overrides.free?.(port) ?? true, pickFree: async () => nextFree++ },
    sleep: async () => undefined,
    throttleMs: 0,
    onEvent: event => events.push(event),
    registryOverride: overrides.registryOverride
  })
  const phases: string[] = []
  const progress: Array<number | null> = []
  controller.onChange(view => {
    if (phases.at(-1) !== view.phase) phases.push(view.phase)
    if (view.phase === "pulling" && progress.at(-1) !== view.progress) progress.push(view.progress)
  })
  const env = () => parseEnvFile(readFileSync(path.join(directory, ".env"), "utf8"))
  return { controller, docker, probes, events, phases, progress, env }
}

describe("local stack controller", () => {
  it("prepares, pulls, starts and verifies a fresh stack, then waits for its administrator", async () => {
    // Nothing answers on the port before the stack runs.
    const { controller, docker, phases, progress, env, probes } = harness({ reports: [null, TAG] })
    await expect(controller.ensureRunning()).resolves.toEqual({ status: "needs-admin", origin: `http://127.0.0.1:${DEFAULT_LOCAL_PORT}` })
    expect(phases).toEqual(["checking", "preparing", "pulling", "starting", "verifying", "needs-admin"])
    expect(docker!.commands()).toEqual(["version --format", "compose version", "compose ps", "image inspect", "compose pull", "compose up", "compose exec accounts list"])
    const values = env()
    expect(values.get("AGORA_WEB_PORT")).toBe(String(DEFAULT_LOCAL_PORT))
    expect(values.get("AGORA_IMAGE_TAG")).toBe(TAG)
    expect(values.get("AGORA_IMAGE_REGISTRY")).toBe("ghcr.io/devin333")
    expect(probes.at(-1)).toEqual({ origin: "http://127.0.0.1:45180", token: values.get("AGORA_DESKTOP_STACK_TOKEN") })
    expect(readFileSync(path.join(directory, "compose.images.yml"), "utf8")).toBe(readFileSync(path.join(RESOURCES, "compose.images.yml"), "utf8"))
    if (process.platform !== "win32") expect(statSync(path.join(directory, ".env")).mode & 0o777).toBe(0o600)
    // Every compose command targets this stack's project and files only.
    const up = docker!.calls.find(call => FakeDocker.key(call.args) === "compose up")!.args
    expect(up.slice(0, 9)).toEqual(["compose", "--project-name", LOCAL_STACK_PROJECT, "--project-directory", directory, "--env-file", path.join(directory, ".env"), "--file", path.join(directory, "compose.images.yml")])
    // The page sees the bar start empty; updates during the download are coalesced.
    expect(progress[0]).toBe(0)
    // Progress means the download only; once it is done the bar goes away.
    expect(controller.view.progress).toBeNull()
    expect(controller.origin).toBe("http://127.0.0.1:45180")
  })

  it("creates the administrator with the password on stdin and remembers it", async () => {
    const { controller, docker } = harness({ reports: [null, TAG] })
    await controller.ensureRunning()
    await expect(controller.createAdmin({ username: "owner", password: "local-pass-1" })).resolves.toEqual({ ok: true })
    const call = docker!.calls.at(-1)!
    expect(call.args.slice(-5)).toEqual(["news", "accounts", "create-admin", "owner", "--password-stdin"])
    expect(call.args.join(" ")).not.toContain("local-pass-1")
    expect(call.input).toBe("local-pass-1\n")
    expect(controller.view.phase).toBe("ready")
    expect(JSON.parse(readFileSync(path.join(directory, "state.json"), "utf8"))).toMatchObject({ adminReady: true, registry: "ghcr" })
  })

  it("shows the server's reason when the administrator cannot be created", async () => {
    const { controller, docker } = harness({ reports: [null, TAG] })
    await controller.ensureRunning()
    docker!.overrides.set("compose exec accounts create-admin", () => ({ code: 1, stderr: '{"error": {"code": "account_exists", "message": "exists"}}' }))
    await expect(controller.createAdmin({ username: "owner", password: "local-pass-1" })).resolves.toMatchObject({ ok: false, code: "account_exists" })
    expect(controller.view.phase).toBe("needs-admin")
  })

  it("opens a running stack of the right release at once, without Docker", async () => {
    const first = harness({ reports: [null, TAG] })
    await first.controller.ensureRunning()
    await first.controller.createAdmin({ username: "owner", password: "local-pass-1" })
    const token = first.env().get("AGORA_DESKTOP_STACK_TOKEN")
    const second = harness({ docker: null })
    await expect(second.controller.ensureRunning()).resolves.toEqual({ status: "ready", origin: "http://127.0.0.1:45180" })
    expect(second.probes).toEqual([{ origin: "http://127.0.0.1:45180", token }])
    expect(second.events).toContain("local_stack_reused")
  })

  it("redeploys a stack running another release and keeps its secrets and port", async () => {
    const first = harness({ reports: [null, TAG] })
    await first.controller.ensureRunning()
    const before = first.env()
    // The old release's containers still hold the port; `up` replaces them and frees it.
    const docker = new FakeDocker()
    docker.overrides.set("compose ps", () => ({ stdout: "abc\n" }))
    const second = harness({ docker, reports: ["v0.1.1", TAG], free: () => false })
    await expect(second.controller.ensureRunning()).resolves.toMatchObject({ status: "needs-admin", origin: "http://127.0.0.1:45180" })
    expect(docker.commands()).toContain("compose up")
    const after = second.env()
    for (const key of ["NEWSROOM_CREDENTIAL_ENCRYPTION_KEY", "NEWS_ACTIVITY_ENCRYPTION_KEY", "AGORA_DESKTOP_STACK_TOKEN"]) expect(after.get(key)).toBe(before.get(key))
    expect(after.get("AGORA_WEB_PORT")).toBe("45180")
  })

  it("picks another port when the default one is taken", async () => {
    const { controller, env } = harness({ reports: [null, TAG], free: port => port !== DEFAULT_LOCAL_PORT })
    await controller.ensureRunning()
    expect(env().get("AGORA_WEB_PORT")).toBe("50000")
    expect(controller.view.port).toBe(50000)
  })

  it("moves to a free port when another program takes the chosen one during the start", async () => {
    const docker = new FakeDocker()
    let attempts = 0
    docker.overrides.set("compose up", () => (++attempts === 1 ? { code: 1, stderr: "Bind for 127.0.0.1:45180 failed: port is already allocated" } : {}))
    const { controller, env } = harness({ docker, reports: [null, TAG] })
    await expect(controller.ensureRunning()).resolves.toMatchObject({ status: "needs-admin", origin: "http://127.0.0.1:50000" })
    expect(attempts).toBe(2)
    expect(env().get("AGORA_WEB_PORT")).toBe("50000")
  })

  it.each([
    ["no Docker installed", null, "docker_missing"],
    ["the daemon stopped", (docker: FakeDocker) => docker.overrides.set("version --format", () => ({ code: 1, stderr: "Cannot connect to the Docker daemon" })), "docker_not_running"],
    ["no permission", (docker: FakeDocker) => docker.overrides.set("version --format", () => ({ code: 1, stderr: "permission denied while trying to connect to the Docker daemon socket" })), "docker_permission"],
    ["a missing release", (docker: FakeDocker) => docker.overrides.set("compose pull", () => ({ code: 1, stderr: "manifest unknown" })), "image_unavailable"],
    ["a dropped download", (docker: FakeDocker) => docker.overrides.set("compose pull", () => ({ code: 1, stderr: "net/http: TLS handshake timeout" })), "image_pull_failed"],
    ["an unhealthy container", (docker: FakeDocker) => docker.overrides.set("compose up", () => ({ code: 1, stderr: "container agora-desktop-api-1 is unhealthy" })), "local_stack_failed"],
    ["a start that never finishes", (docker: FakeDocker) => docker.overrides.set("compose up", () => ({ code: null, timedOut: true })), "local_stack_timeout"]
  ])("reports %s", async (_name, setup, code) => {
    const docker = setup === null ? null : new FakeDocker()
    if (docker && setup) setup(docker)
    const { controller } = harness({ docker, reports: [null] })
    await expect(controller.ensureRunning()).resolves.toMatchObject({ status: "failed", failure: { code } })
    expect(controller.view).toMatchObject({ phase: "failed", failure: { code } })
  })

  it("adds the containers' last log lines when the start fails", async () => {
    const docker = new FakeDocker()
    docker.overrides.set("compose up", () => ({ code: 1, stderr: "container agora-desktop-api-1 is unhealthy" }))
    docker.overrides.set("compose logs", () => ({ stdout: "api-1  | NEWSROOM_CREDENTIAL_ENCRYPTION_KEY: must be 32 bytes\napi-1  | token=abc123\n" }))
    const { controller } = harness({ docker, reports: [null] })
    await controller.ensureRunning()
    expect(controller.view.output).toContain("api-1  | NEWSROOM_CREDENTIAL_ENCRYPTION_KEY: must be 32 bytes")
    expect(controller.view.output.join("\n")).not.toContain("abc123")
  })

  it("reports a stack that answers with another release after starting", async () => {
    const { controller } = harness({ reports: [null, "v0.0.9"] })
    await expect(controller.ensureRunning()).resolves.toMatchObject({ status: "failed", failure: { code: "local_stack_failed" } })
    expect(controller.view.output.at(-1)).toBe("本机服务运行的版本是 v0.0.9，这个应用需要 v0.1.2。")
  })

  it("does not pull a pinned release that is already on this computer", async () => {
    const docker = new FakeDocker()
    docker.imagesPresent = true
    const { controller } = harness({ docker, reports: [null, TAG] })
    await controller.ensureRunning()
    expect(docker.commands()).not.toContain("compose pull")
  })

  it("interrupts a download the user cancels and shares one run between callers", async () => {
    const docker = new FakeDocker()
    docker.overrides.set("compose pull", (_args, options) => new Promise(resolve => {
      options.signal?.addEventListener("abort", () => resolve({ code: null, aborted: true }))
    }))
    const { controller } = harness({ docker, reports: [null] })
    const first = controller.ensureRunning()
    const second = controller.ensureRunning()
    expect(controller.busy).toBe(true)
    await new Promise(resolve => setTimeout(resolve, 10))
    controller.cancel()
    await expect(first).resolves.toEqual({ status: "cancelled" })
    await expect(second).resolves.toEqual({ status: "cancelled" })
    expect(controller.busy).toBe(false)
    expect(controller.view.phase).toBe("idle")
  })

  it("stops the containers on request and keeps the chosen registry", async () => {
    const { controller, docker } = harness({ reports: [null, TAG] })
    await controller.setRegistry("acr")
    await controller.ensureRunning()
    await expect(controller.stop()).resolves.toEqual({ ok: true })
    expect(docker!.commands().at(-1)).toBe("compose stop")
    expect(controller.view.phase).toBe("stopped")
    expect(readFileSync(path.join(directory, ".env"), "utf8")).toContain("AGORA_IMAGE_REGISTRY=registry.cn-hangzhou.aliyuncs.com/agora-hub")
  })

  it("uses a development registry in place of the chosen one", async () => {
    const { controller, env } = harness({ reports: [null, TAG], registryOverride: "local" })
    await controller.ensureRunning()
    expect(env().get("AGORA_IMAGE_REGISTRY")).toBe("local")
  })
})

describe("image download progress", () => {
  it("adds up every layer's bytes and reports each finished step once", () => {
    const progress = new PullProgress()
    expect(progress.consume('{"id":"Image api","status":"Working","text":"Pulling"}')).toEqual({ progress: null, line: "Image api Pulling" })
    expect(progress.consume('{"id":"a","parent_id":"Image api","text":"Downloading","current":25,"total":100}')).toEqual({ progress: 0.25, line: null })
    expect(progress.consume('{"id":"b","parent_id":"Image web","text":"Downloading","current":0,"total":300}')).toEqual({ progress: 25 / 400, line: null })
    expect(progress.consume('{"id":"a","parent_id":"Image api","text":"Download complete"}').progress).toBe(100 / 400)
    expect(progress.consume('{"id":"a","parent_id":"Image api","text":"Download complete"}').line).toBeNull()
    expect(progress.consume("plain warning line")).toEqual({ progress: null, line: "plain warning line" })
  })
})
