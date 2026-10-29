import { readFile, mkdir, writeFile, rename, rm, chmod } from "node:fs/promises"
import { createHash, randomUUID } from "node:crypto"
import { createServer } from "node:net"
import path from "node:path"
import type { LocalImageRegistryId, LocalStackPhase, LocalStackView } from "./contracts"
import { classifyDockerFailure, type DockerCommandOptions, type DockerCommandResult, type DockerRunner } from "./docker-cli"
import { readPreferenceDocument, writePreferenceDocument } from "./host-preferences"
import { connectionFailure, type ConnectionFailure, type ConnectionFailureCode } from "./load-failure"
import { accountCount, adminFailure, createAdminArguments, listAccountsArguments, type LocalAdminInput } from "./local-stack-admin"
import { mergeStackEnv, parseEnvFile, parsePort, renderEnvFile, type StackEnv } from "./local-stack-env"
import { redactText } from "./redaction"

/**
 * The stack the app runs on this computer in local mode: the published api and
 * web images under Docker Compose, the web server on one loopback port.
 *
 * The app owns everything about it: it writes the compose file and `.env` to
 * `userData/stack`, generates the stack's secrets once, picks the port, pulls
 * the images of the release it was built for and starts the containers. Before
 * the window opens the stack, the stack must prove it holds this app's token
 * and runs that release (`/.well-known/agora-desktop-stack`); a stack that
 * already does so is opened at once, without touching Docker, so a normal
 * launch is as fast as connecting to a server. The containers outlive the app:
 * quitting only interrupts a start in progress, stopping is an explicit action.
 */
export const LOCAL_STACK_PROJECT = "agora-desktop"
export const DEFAULT_LOCAL_PORT = 45180

/**
 * The compose project of a test profile: each one runs its own stack, so a test
 * never stops, replaces or reads the stack the installed app manages.
 */
export function testStackProjectName(userDataPath: string): string {
  return `${LOCAL_STACK_PROJECT}-test-${createHash("sha256").update(userDataPath).digest("hex").slice(0, 10)}`
}
export const IDENTITY_PATH = "/.well-known/agora-desktop-stack"
export const IDENTITY_HEADER = "x-agora-desktop-stack-token"
export const COMPOSE_FILE = "compose.images.yml"
/** deploy/desktop/env.template; without a leading dot so packagers keep it. */
export const ENV_TEMPLATE_FILE = "env.template"
export const ENV_FILE = ".env"
export const STATE_FILE = "state.json"
const STATE_SCHEMA = "agora.desktop-stack/v1"
const IMAGES = ["agora-api", "agora-web"] as const
/** Tags that move; their images are pulled again on every slow start. */
const MOVING_TAGS = new Set(["edge"])
const OUTPUT_LINES = 200
const PORT_ATTEMPTS = 3

export const IMAGE_REGISTRIES: ReadonlyArray<{ id: LocalImageRegistryId; label: string; path: string }> = Object.freeze([
  { id: "ghcr", label: "GitHub 镜像仓库（海外网络）", path: "ghcr.io/devin333" },
  { id: "acr", label: "阿里云镜像仓库（中国大陆网络）", path: "registry.cn-hangzhou.aliyuncs.com/agora-hub" }
])

export const TIMEOUTS = Object.freeze({
  dockerCheckMs: 20_000,
  identityMs: 2_000,
  pullMs: 30 * 60_000,
  upMs: 330_000,
  execMs: 90_000,
  stopMs: 120_000,
  logsMs: 30_000,
  shortMs: 30_000
})
const VERIFY_ATTEMPTS = 15
const VERIFY_INTERVAL_MS = 1_000

export type LocalStackResult =
  | { status: "ready"; origin: string }
  | { status: "needs-admin"; origin: string }
  | { status: "failed"; failure: ConnectionFailure }
  | { status: "cancelled" }

export type LocalAdminResult = { ok: true } | { ok: false; code: string; message: string }

export interface LocalStackPorts {
  isFree(port: number): Promise<boolean>
  pickFree(): Promise<number>
}

export interface LocalStackOptions {
  /** Where the compose file, `.env` and state live (`userData/stack`). */
  directory: string
  /** The packaged copies of deploy/desktop's compose file and template. */
  resourceDirectory: string
  /** Compose project name; separate test profiles get their own so they never touch the user's stack. */
  projectName?: string
  /** The release this app runs, e.g. v0.1.2. */
  imageTag: string
  /** Development only: a registry path that replaces the chosen one. */
  registryOverride?: string | null
  /** The Docker CLI, looked up again on every start so a fresh install is found without restarting the app. */
  docker(): DockerRunner | null
  /** The release the stack on `origin` reports for `token`, or null when nothing there answers for it. */
  probeIdentity(origin: string, token: string, timeoutMs: number): Promise<string | null>
  ports: LocalStackPorts
  sleep(ms: number, signal: AbortSignal): Promise<void>
  /** Coalesces progress updates; phase changes are reported at once. */
  throttleMs?: number
  onEvent?(event: string, details: Record<string, unknown>): void
}

class StackFailure extends Error {
  constructor(readonly code: ConnectionFailureCode) {
    super(code)
  }
}

class StackCancelled extends Error {
  constructor() {
    super("cancelled")
  }
}

export function localOrigin(port: number): string {
  return `http://127.0.0.1:${port}`
}

export function isRegistryId(value: unknown): value is LocalImageRegistryId {
  return IMAGE_REGISTRIES.some(registry => registry.id === value)
}

export class LocalStackController {
  private phase: LocalStackPhase = "idle"
  private progress: number | null = null
  private output: string[] = []
  private failure: ConnectionFailure | null = null
  private port: number | null = null
  private registry: LocalImageRegistryId = "ghcr"
  private adminReady = false
  private running: Promise<LocalStackResult> | null = null
  private abort: AbortController | null = null
  private emitHandle: ReturnType<typeof setTimeout> | null = null
  private readonly listeners = new Set<(view: LocalStackView) => void>()

  constructor(private readonly options: LocalStackOptions) {
    const state = readPreferenceDocument(this.statePath, STATE_SCHEMA)
    if (state) {
      if (isRegistryId(state.registry)) this.registry = state.registry
      this.adminReady = state.adminReady === true
    }
  }

  get view(): LocalStackView {
    return {
      phase: this.phase,
      progress: this.progress,
      output: [...this.output],
      failure: this.failure,
      registry: this.registry,
      registries: IMAGE_REGISTRIES.map(({ id, label }) => ({ id, label })),
      imageTag: this.options.imageTag,
      port: this.port
    }
  }

  /** The loopback origin of the stack while it is ready to be opened. */
  get origin(): string | null {
    return (this.phase === "ready" || this.phase === "needs-admin" || this.phase === "creating-admin") && this.port ? localOrigin(this.port) : null
  }

  get busy(): boolean {
    return this.running !== null
  }

  onChange(listener: (view: LocalStackView) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  async setRegistry(registry: LocalImageRegistryId): Promise<void> {
    if (registry === this.registry) return
    this.registry = registry
    await this.saveState()
    this.emit(true)
  }

  /** Brings the stack up (or confirms it is up); concurrent callers share one run. */
  ensureRunning(): Promise<LocalStackResult> {
    if (!this.running) {
      this.running = this.run().finally(() => {
        this.running = null
        this.abort = null
      })
    }
    return this.running
  }

  /** Interrupts a start in progress. Containers already running keep running. */
  cancel(): void {
    this.abort?.abort()
  }

  async createAdmin(input: LocalAdminInput): Promise<LocalAdminResult> {
    const docker = this.options.docker()
    if (this.phase !== "needs-admin" || !docker) return { ok: false, code: "admin_unavailable", message: "本机服务还没有准备好，请稍后重试。" }
    this.setPhase("creating-admin")
    const result = await this.compose(docker, ["exec", "-T", "api", ...createAdminArguments(input.username)], {
      timeoutMs: TIMEOUTS.execMs,
      input: `${input.password}\n`
    })
    if (result.code !== 0) {
      this.setPhase("needs-admin")
      const failure = adminFailure(result.stderr)
      this.options.onEvent?.("local_stack_admin_failed", { code: failure.code })
      return { ok: false, ...failure }
    }
    this.adminReady = true
    await this.saveState()
    this.options.onEvent?.("local_stack_admin_created", {})
    this.setPhase("ready")
    return { ok: true }
  }

  async stop(): Promise<{ ok: boolean }> {
    this.cancel()
    await this.running?.catch(() => undefined)
    const docker = this.options.docker()
    if (!docker || !(await this.hasStackFiles())) {
      this.setPhase("stopped")
      return { ok: true }
    }
    this.setPhase("stopping")
    const result = await this.compose(docker, ["stop"], { timeoutMs: TIMEOUTS.stopMs })
    this.options.onEvent?.("local_stack_stopped", { ok: result.code === 0 })
    if (result.code !== 0) {
      this.fail(classifyDockerFailure(`${result.stderr}\n${result.stdout}`))
      return { ok: false }
    }
    this.setPhase("stopped")
    return { ok: true }
  }

  /** The containers' recent logs, with secrets removed, for the user to read. */
  async logs(): Promise<string> {
    const docker = this.options.docker()
    if (!docker) return connectionFailure("docker_missing").message
    if (!(await this.hasStackFiles())) return "本机服务还没有启动过。"
    const result = await this.compose(docker, ["logs", "--no-color", "--timestamps", "--tail", "1000"], { timeoutMs: TIMEOUTS.logsMs })
    const text = `${result.stdout}${result.stderr}`.trim()
    return text.split(/\r?\n/).map(line => redactText(line, 2000)).join("\n") || "本机服务暂时没有日志。"
  }

  dispose(): void {
    this.cancel()
    if (this.emitHandle) clearTimeout(this.emitHandle)
    this.emitHandle = null
    this.listeners.clear()
  }

  private get statePath(): string {
    return path.join(this.options.directory, STATE_FILE)
  }

  private get envPath(): string {
    return path.join(this.options.directory, ENV_FILE)
  }

  private get composePath(): string {
    return path.join(this.options.directory, COMPOSE_FILE)
  }

  private get registryPath(): string {
    return this.options.registryOverride || IMAGE_REGISTRIES.find(registry => registry.id === this.registry)!.path
  }

  private async run(): Promise<LocalStackResult> {
    const abort = new AbortController()
    this.abort = abort
    this.output = []
    this.failure = null
    this.progress = null
    try {
      this.setPhase("checking")
      const fast = await this.openRunningStack(abort.signal)
      if (fast) return fast
      const docker = await this.checkDocker(abort.signal)
      this.setPhase("preparing")
      let env = await this.prepare(docker, abort.signal)
      await this.pullImages(docker, env, abort.signal)
      env = await this.start(docker, env, abort.signal)
      await this.verify(env, abort.signal)
      return await this.settle(docker, env, abort.signal)
    } catch (error) {
      if (error instanceof StackCancelled || abort.signal.aborted) {
        this.progress = null
        this.setPhase("idle")
        this.options.onEvent?.("local_stack_cancelled", {})
        return { status: "cancelled" }
      }
      const code = error instanceof StackFailure ? error.code : "local_stack_failed"
      if (!(error instanceof StackFailure)) this.appendOutput(redactText(error instanceof Error ? error.message : error, 500))
      this.fail(code)
      return { status: "failed", failure: this.failure! }
    }
  }

  /** The fast path: a stack already up with this app's token and release is opened without Docker. */
  private async openRunningStack(signal: AbortSignal): Promise<LocalStackResult | null> {
    const env = await this.readEnv()
    const port = parsePort(env.get("AGORA_WEB_PORT"))
    const token = env.get("AGORA_DESKTOP_STACK_TOKEN")
    if (!port || !token) return null
    this.port = port
    const tag = await this.options.probeIdentity(localOrigin(port), token, TIMEOUTS.identityMs)
    this.throwIfAborted(signal)
    if (tag !== this.options.imageTag) return null
    this.options.onEvent?.("local_stack_reused", { port })
    if (this.adminReady) {
      this.setPhase("ready")
      return { status: "ready", origin: localOrigin(port) }
    }
    const docker = await this.checkDocker(signal)
    return await this.settle(docker, await this.currentEnv(), signal)
  }

  private async checkDocker(signal: AbortSignal): Promise<DockerRunner> {
    this.setPhase("checking")
    const docker = this.options.docker()
    if (!docker) throw new StackFailure("docker_missing")
    const server = await this.command(docker, ["version", "--format", "{{.Server.Version}}"], { timeoutMs: TIMEOUTS.dockerCheckMs, signal })
    if (server.timedOut) throw new StackFailure("docker_not_running")
    if (server.code !== 0) {
      const code = classifyDockerFailure(`${server.stderr}\n${server.stdout}`)
      throw new StackFailure(code === "docker_permission" ? code : "docker_not_running")
    }
    const compose = await this.command(docker, ["compose", "version", "--short"], { timeoutMs: TIMEOUTS.dockerCheckMs, signal })
    if (compose.code !== 0) throw new StackFailure("docker_missing")
    this.appendOutput(`Docker ${server.stdout.trim()}，Compose ${compose.stdout.trim()}`)
    return docker
  }

  /** Writes the compose file and `.env`, keeping the secrets, and settles the port. */
  private async prepare(docker: DockerRunner, signal: AbortSignal): Promise<StackEnv> {
    await mkdir(this.options.directory, { recursive: true })
    const [compose, template] = await Promise.all([
      readFile(path.join(this.options.resourceDirectory, COMPOSE_FILE), "utf8"),
      readFile(path.join(this.options.resourceDirectory, ENV_TEMPLATE_FILE), "utf8")
    ])
    await this.writeAtomically(this.composePath, compose)
    const existing = await this.readEnv()
    let port = parsePort(existing.get("AGORA_WEB_PORT"))
    // The stack's own containers may hold the port; `up` recreates them and frees it.
    const containers = await this.compose(docker, ["ps", "--all", "--quiet"], { timeoutMs: TIMEOUTS.shortMs, signal }, true)
    const ours = containers.code === 0 && containers.stdout.trim() !== ""
    if (port === null) port = (await this.options.ports.isFree(DEFAULT_LOCAL_PORT)) ? DEFAULT_LOCAL_PORT : await this.options.ports.pickFree()
    else if (!ours && !(await this.options.ports.isFree(port))) port = await this.options.ports.pickFree()
    this.throwIfAborted(signal)
    const env = mergeStackEnv(existing, { AGORA_WEB_PORT: String(port), AGORA_IMAGE_REGISTRY: this.registryPath, AGORA_IMAGE_TAG: this.options.imageTag })
    await this.writeEnv(template, env)
    this.port = port
    return env
  }

  private async pullImages(docker: DockerRunner, env: StackEnv, signal: AbortSignal): Promise<void> {
    const images = IMAGES.map(name => `${env.AGORA_IMAGE_REGISTRY}/${name}:${env.AGORA_IMAGE_TAG}`)
    const present = (await this.command(docker, ["image", "inspect", "--format", "{{.Id}}", ...images], { timeoutMs: TIMEOUTS.shortMs, signal })).code === 0
    if (present && !MOVING_TAGS.has(env.AGORA_IMAGE_TAG)) return
    this.progress = 0
    this.setPhase("pulling")
    const tracker = new PullProgress()
    const result = await this.compose(docker, ["--progress", "json", "pull"], {
      timeoutMs: TIMEOUTS.pullMs,
      signal,
      onLine: line => {
        const update = tracker.consume(line)
        if (update.line) this.appendOutput(update.line)
        if (update.progress !== null) this.progress = update.progress
        this.emit(false)
      }
    })
    this.throwIfAborted(signal)
    if (result.code === 0) {
      this.progress = 1
      return
    }
    if (present) {
      // A moving tag that cannot be refreshed still has a working local copy.
      this.appendOutput("没能更新服务镜像，继续使用这台电脑上已有的版本。")
      return
    }
    throw new StackFailure(result.timedOut ? "image_pull_failed" : classifyDockerFailure(`${result.stderr}\n${result.stdout}`))
  }

  private async start(docker: DockerRunner, env: StackEnv, signal: AbortSignal): Promise<StackEnv> {
    this.progress = null
    this.setPhase("starting")
    let current = env
    for (let attempt = 1; ; attempt += 1) {
      const result = await this.compose(docker, ["up", "--detach", "--wait", "--wait-timeout", "300", "--remove-orphans"], { timeoutMs: TIMEOUTS.upMs, signal })
      this.throwIfAborted(signal)
      if (result.code === 0) return current
      const code = result.timedOut ? "local_stack_timeout" : classifyDockerFailure(`${result.stderr}\n${result.stdout}`)
      if (code === "local_port_in_use" && attempt < PORT_ATTEMPTS) {
        // Another program took the port between the check and the start.
        const port = await this.options.ports.pickFree()
        this.appendOutput(`端口 ${current.AGORA_WEB_PORT} 被占用，改用 ${port}。`)
        current = { ...current, AGORA_WEB_PORT: String(port) }
        await this.writeEnv(await readFile(path.join(this.options.resourceDirectory, ENV_TEMPLATE_FILE), "utf8"), current)
        this.port = port
        continue
      }
      await this.appendContainerLogs(docker)
      throw new StackFailure(code === "image_pull_failed" || code === "image_unavailable" ? "local_stack_failed" : code)
    }
  }

  private async verify(env: StackEnv, signal: AbortSignal): Promise<void> {
    this.setPhase("verifying")
    const origin = localOrigin(Number(env.AGORA_WEB_PORT))
    let reported: string | null = null
    for (let attempt = 0; attempt < VERIFY_ATTEMPTS; attempt += 1) {
      reported = await this.options.probeIdentity(origin, env.AGORA_DESKTOP_STACK_TOKEN, TIMEOUTS.identityMs)
      this.throwIfAborted(signal)
      if (reported === this.options.imageTag) return
      if (reported !== null) break
      await this.options.sleep(VERIFY_INTERVAL_MS, signal)
      this.throwIfAborted(signal)
    }
    this.appendOutput(reported === null
      ? `本机服务没有在 ${origin} 应答。`
      : `本机服务运行的版本是 ${reported}，这个应用需要 ${this.options.imageTag}。`)
    throw new StackFailure("local_stack_failed")
  }

  /** Ready, or waiting for its first administrator. */
  private async settle(docker: DockerRunner, env: StackEnv, signal: AbortSignal): Promise<LocalStackResult> {
    const origin = localOrigin(Number(env.AGORA_WEB_PORT))
    if (!this.adminReady) {
      const result = await this.compose(docker, ["exec", "-T", "api", ...listAccountsArguments()], { timeoutMs: TIMEOUTS.execMs, signal })
      this.throwIfAborted(signal)
      const count = result.code === 0 ? accountCount(result.stdout) : null
      if (count === null) {
        this.appendOutput(redactText(result.stderr.trim(), 500))
        throw new StackFailure("local_stack_failed")
      }
      if (count === 0) {
        this.setPhase("needs-admin")
        return { status: "needs-admin", origin }
      }
      this.adminReady = true
      await this.saveState()
    }
    this.options.onEvent?.("local_stack_ready", { port: Number(env.AGORA_WEB_PORT) })
    this.setPhase("ready")
    return { status: "ready", origin }
  }

  private async appendContainerLogs(docker: DockerRunner): Promise<void> {
    const logs = await this.compose(docker, ["logs", "--no-color", "--tail", "30"], { timeoutMs: TIMEOUTS.logsMs })
    for (const line of `${logs.stdout}${logs.stderr}`.split(/\r?\n/)) if (line.trim()) this.appendOutput(line)
  }

  private composeArguments(): string[] {
    return ["compose", "--project-name", this.options.projectName ?? LOCAL_STACK_PROJECT, "--project-directory", this.options.directory, "--env-file", this.envPath, "--file", this.composePath]
  }

  /**
   * `docker compose` against this stack. Every argument of
   * {@link composeArguments} is a global flag, so callers may start `args`
   * with further global flags (`--progress json`) before the subcommand.
   */
  private compose(docker: DockerRunner, args: string[], options: DockerCommandOptions, quiet = false): Promise<DockerCommandResult> {
    return this.command(docker, [...this.composeArguments(), ...args], { cwd: this.options.directory, ...options }, quiet)
  }

  private async command(docker: DockerRunner, args: string[], options: DockerCommandOptions, quiet = false): Promise<DockerCommandResult> {
    const result = await docker(args, {
      ...options,
      onLine: options.onLine ?? (quiet ? undefined : line => this.appendOutput(line))
    })
    if (result.aborted) throw new StackCancelled()
    return result
  }

  private throwIfAborted(signal: AbortSignal): void {
    if (signal.aborted) throw new StackCancelled()
  }

  private async readEnv(): Promise<Map<string, string>> {
    try {
      return parseEnvFile(await readFile(this.envPath, "utf8"))
    } catch {
      return new Map()
    }
  }

  private async currentEnv(): Promise<StackEnv> {
    const env = await this.readEnv()
    return mergeStackEnv(env, { AGORA_IMAGE_REGISTRY: env.get("AGORA_IMAGE_REGISTRY") || this.registryPath, AGORA_IMAGE_TAG: this.options.imageTag })
  }

  private async writeEnv(template: string, env: StackEnv): Promise<void> {
    await this.writeAtomically(this.envPath, renderEnvFile(template, env), 0o600)
  }

  private async writeAtomically(filePath: string, text: string, mode = 0o644): Promise<void> {
    const temporary = `${filePath}.${randomUUID()}.tmp`
    try {
      await writeFile(temporary, text, { encoding: "utf8", mode, flag: "wx" })
      await rename(temporary, filePath)
      // rename keeps the temporary file's mode; set it again where umask narrowed it differently.
      await chmod(filePath, mode).catch(() => undefined)
    } catch (error) {
      await rm(temporary, { force: true })
      throw error
    }
  }

  private async hasStackFiles(): Promise<boolean> {
    try {
      await Promise.all([readFile(this.composePath), readFile(this.envPath)])
      return true
    } catch {
      return false
    }
  }

  private async saveState(): Promise<void> {
    await writePreferenceDocument(this.statePath, STATE_SCHEMA, { registry: this.registry, adminReady: this.adminReady })
  }

  private appendOutput(line: string): void {
    const text = redactText(line.trimEnd(), 500)
    if (!text) return
    this.output.push(text)
    if (this.output.length > OUTPUT_LINES) this.output.splice(0, this.output.length - OUTPUT_LINES)
    this.emit(false)
  }

  private fail(code: ConnectionFailureCode): void {
    this.failure = connectionFailure(code)
    this.progress = null
    this.options.onEvent?.("local_stack_failed", { code })
    this.setPhase("failed")
  }

  private setPhase(phase: LocalStackPhase): void {
    if (phase !== "failed") this.failure = null
    this.phase = phase
    this.emit(true)
  }

  private emit(immediate: boolean): void {
    if (!immediate) {
      if (this.emitHandle) return
      this.emitHandle = setTimeout(() => {
        this.emitHandle = null
        this.notify()
      }, this.options.throttleMs ?? 150)
      return
    }
    if (this.emitHandle) clearTimeout(this.emitHandle)
    this.emitHandle = null
    this.notify()
  }

  private notify(): void {
    const view = this.view
    for (const listener of this.listeners) listener(view)
  }
}

/**
 * Reads `docker compose --progress json pull` events: the share of bytes
 * downloaded across every layer, and one readable line per step a layer or
 * image finishes (not per progress tick).
 */
export class PullProgress {
  private readonly layers = new Map<string, { current: number; total: number }>()
  private readonly steps = new Map<string, string>()

  consume(raw: string): { progress: number | null; line: string | null } {
    let event: { id?: unknown; text?: unknown; current?: unknown; total?: unknown; status?: unknown; parent_id?: unknown }
    try {
      event = JSON.parse(raw)
    } catch {
      return { progress: null, line: raw.trim() || null }
    }
    const id = typeof event.id === "string" ? event.id : ""
    const text = typeof event.text === "string" ? event.text : ""
    if (!id) return { progress: null, line: text || null }
    if (typeof event.total === "number" && event.total > 0 && event.parent_id) {
      const current = typeof event.current === "number" ? Math.min(event.current, event.total) : 0
      this.layers.set(id, { current, total: event.total })
    } else if (event.parent_id && /complete|exists/i.test(text)) {
      const layer = this.layers.get(id)
      if (layer) layer.current = layer.total
    }
    const line = text && text !== "Downloading" && text !== "Extracting" && this.steps.get(id) !== text ? `${id} ${text}` : null
    if (line) this.steps.set(id, text)
    return { progress: this.share(), line }
  }

  private share(): number | null {
    let current = 0
    let total = 0
    for (const layer of this.layers.values()) {
      current += layer.current
      total += layer.total
    }
    return total > 0 ? current / total : null
  }
}

/** A free loopback port, or whether one is. */
export const systemPorts: LocalStackPorts = {
  isFree: port => new Promise(resolve => {
    const server = createServer()
    server.once("error", () => resolve(false))
    server.listen({ port, host: "127.0.0.1", exclusive: true }, () => server.close(() => resolve(true)))
  }),
  pickFree: () => new Promise((resolve, reject) => {
    const server = createServer()
    server.once("error", reject)
    server.listen({ port: 0, host: "127.0.0.1", exclusive: true }, () => {
      const address = server.address()
      const port = typeof address === "object" && address ? address.port : 0
      server.close(() => port ? resolve(port) : reject(new Error("no free port")))
    })
  })
}

export type IdentityFetch = (url: string, init: { signal: AbortSignal; redirect: "manual"; cache: "no-store"; headers: Record<string, string> }) => Promise<{ status: number; text(): Promise<string> }>

/** Asks `origin` which release it runs, presenting the stack token; null for anything but a matching stack. */
export async function probeLocalStackIdentity(fetch: IdentityFetch, origin: string, token: string, timeoutMs: number): Promise<string | null> {
  try {
    const response = await fetch(new URL(IDENTITY_PATH, origin).href, {
      signal: AbortSignal.timeout(timeoutMs),
      redirect: "manual",
      cache: "no-store",
      headers: { Accept: "application/json", [IDENTITY_HEADER]: token }
    })
    if (response.status !== 200) return null
    const document = JSON.parse(await response.text()) as { ok?: unknown; imageTag?: unknown }
    return document.ok === true && typeof document.imageTag === "string" ? document.imageTag : null
  } catch {
    return null
  }
}

export function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise(resolve => {
    if (signal.aborted) return resolve()
    const handle = setTimeout(done, ms)
    function done() {
      clearTimeout(handle)
      signal.removeEventListener("abort", done)
      resolve()
    }
    signal.addEventListener("abort", done, { once: true })
  })
}
