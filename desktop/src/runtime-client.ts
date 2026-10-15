import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process"
import { EventEmitter } from "node:events"
import { RUNTIME_COMMANDS, type RuntimeCliResponse, type RuntimeCommand } from "./contracts"
import { redactText } from "./redaction"
import type { RuntimeLayout } from "./runtime-layout"

const STDOUT_LIMIT = 256 * 1024
const STDERR_LIMIT = 64 * 1024

export const COMMAND_TIMEOUTS: Readonly<Record<RuntimeCommand, number>> = Object.freeze({
  status: 12_000,
  doctor: 45_000,
  // Interim finite envelope: the current 42,539-resource source build measured about six
  // minutes before health readiness. Re-profile after owner lstat/cache optimization and
  // replace this with phase-aware deadlines when the CLI progress contract is frozen.
  start: 10 * 60_000,
  stop: 75_000,
  backup: 180_000,
  restore: 300_000,
  "config-show": 30_000,
  "config-preview": 30_000,
  // Finite apply envelope for the measured 42,539-file release, not a startup promise.
  // Six full verification passes (6 * 90 s), rebind I/O (180 s), owner drain (60 s),
  // rollback verification (90 s), and command/lock overhead (30 s). Re-profile the
  // real apply chain before release; a timeout remains UNKNOWN, never APPLIED.
  "config-apply": 900_000,
  "credential-inspect": 15_000,
  "credential-prompt": 300_000,
  "credential-delete": 300_000,
  "data-clear-preview": 5 * 60_000,
  "data-clear-apply": 5 * 60_000,
  "data-clear-status": 20_000,
  "data-clear-finalize": 35 * 60_000
})

const TERMINATE_GRACE_MS = 5_000
const FORCE_KILL_GRACE_MS = 5_000
const STDIN_LIMIT = 16 * 1024
const FINALIZER_READY_TIMEOUT_MS = 20_000

export interface SpawnedProcess extends EventEmitter {
  stdin?: NodeJS.WritableStream
  stdout: NodeJS.ReadableStream
  stderr: NodeJS.ReadableStream
  kill(signal?: NodeJS.Signals | number): boolean
  unref?(): void
}

export type SpawnRuntime = (
  command: string,
  args: readonly string[],
  options: { cwd: string; env: NodeJS.ProcessEnv; windowsHide: boolean; shell: false; detached?: boolean }
) => SpawnedProcess

export interface RuntimeClientTiming {
  commandTimeouts?: Partial<Record<RuntimeCommand, number>>
  terminateGraceMs?: number
  forceKillGraceMs?: number
}

export interface RuntimeInvokeOptions {
  backupDestination?: string
  restoreBackupPath?: string
  activate?: boolean
  personalMemoryConfirmation?: string
  configCandidate?: unknown
  expectedRevision?: number
  confirmationToken?: string
  parentWindow?: bigint
}

export interface RuntimeInvocationErrorOptions {
  command: RuntimeCommand
  stderr?: string
  exitCode?: number | null
}

export class RuntimeInvocationError extends Error {
  readonly command: RuntimeCommand
  readonly stderr: string
  readonly exitCode: number | null

  constructor(readonly code: string, message: string, options: RuntimeInvocationErrorOptions) {
    super(message)
    this.name = "RuntimeInvocationError"
    this.command = options.command
    this.stderr = redactText(options.stderr ?? "", 2000)
    this.exitCode = options.exitCode ?? null
  }
}

export function buildPythonEnvironment(source: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = {}
  for (const [key, value] of Object.entries(source)) {
    const normalized = key.toUpperCase()
    if (normalized === "PYTHONHOME" || normalized === "PYTHONPATH" || normalized === "NEWSROOM_DESKTOP_RESEARCH_API_KEY" || value === undefined) continue
    environment[key] = value
  }
  environment.PYTHONNOUSERSITE = "1"
  environment.PYTHONDONTWRITEBYTECODE = "1"
  environment.PYTHONUNBUFFERED = "1"
  return environment
}

function defaultSpawn(command: string, args: readonly string[], options: Parameters<SpawnRuntime>[2]): SpawnedProcess {
  return spawn(command, args, { ...options, stdio: ["pipe", "pipe", "pipe"] }) as unknown as ChildProcessWithoutNullStreams
}

function appendBounded(current: Buffer<ArrayBufferLike>, chunk: Buffer<ArrayBufferLike>, limit: number): { value: Buffer<ArrayBufferLike>; overflow: boolean } {
  if (current.length + chunk.length <= limit) return { value: Buffer.concat([current, chunk]), overflow: false }
  return { value: Buffer.concat([current, chunk]).subarray(0, limit), overflow: true }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
}

export function parseRuntimeResponse(raw: string, expectedCommand: RuntimeCommand): RuntimeCliResponse {
  let value: unknown
  try {
    value = JSON.parse(raw.trim())
  } catch {
    throw new RuntimeInvocationError("runtime_protocol_invalid_json", "Runtime returned invalid JSON", { command: expectedCommand })
  }
  if (!isRecord(value) || typeof value.ok !== "boolean" || value.command !== expectedCommand || typeof value.state !== "string" || typeof value.code !== "string" || typeof value.message !== "string" || !isRecord(value.data)) {
    throw new RuntimeInvocationError("runtime_protocol_invalid_shape", "Runtime response does not match the desktop protocol", { command: expectedCommand })
  }
  if (!RUNTIME_COMMANDS.includes(value.command as RuntimeCommand)) {
    throw new RuntimeInvocationError("runtime_protocol_invalid_command", "Runtime returned an unsupported command", { command: expectedCommand })
  }
  if (!/^[A-Z][A-Z0-9_]{0,63}$/.test(value.state) || !/^[a-z][a-z0-9_]{0,127}$/.test(value.code) || value.message.length > 2000 || /[\u0000\r\n]/.test(value.message)) {
    throw new RuntimeInvocationError("runtime_protocol_invalid_metadata", "Runtime response metadata is not bounded", { command: expectedCommand })
  }
  return { ...value, message: redactText(value.message, 1000) } as unknown as RuntimeCliResponse
}

export class RuntimeClient {
  constructor(
    readonly layout: RuntimeLayout,
    private readonly spawnRuntime: SpawnRuntime = defaultSpawn,
    private readonly environment: NodeJS.ProcessEnv = process.env,
    private readonly timing: RuntimeClientTiming = {}
  ) {}

  async invoke(command: RuntimeCommand, options: RuntimeInvokeOptions = {}): Promise<RuntimeCliResponse> {
    if (command === "data-clear-apply" || command === "data-clear-finalize") {
      throw new RuntimeInvocationError("runtime_stdin_required", `${command} requires the private bounded stdin transport`, { command })
    }
    return await this.invokeBounded(command, options)
  }

  async invokePrivate(command: "data-clear-apply", payload: unknown): Promise<RuntimeCliResponse> {
    return await this.invokeBounded(command, {}, payload)
  }

  async launchDataClearFinalizer(payload: unknown): Promise<void> {
    const command = "data-clear-finalize" as const
    const encoded = Buffer.from(`${JSON.stringify(payload)}\n`, "utf8")
    if (encoded.length > STDIN_LIMIT) throw new RuntimeInvocationError("runtime_stdin_limit", "Finalizer stdin request exceeded the protocol limit", { command })
    const request = isRecord(payload) ? payload : null
    const operationId = request?.operationId
    const readyNonce = request?.readyNonce
    if (typeof operationId !== "string" || !/^[0-9a-f]{32}$/.test(operationId) || typeof readyNonce !== "string" || !/^[A-Za-z0-9_-]{20,128}$/.test(readyNonce)) {
      throw new RuntimeInvocationError("runtime_finalizer_request_invalid", "Finalizer readiness identity is invalid", { command })
    }
    const args = [
      "-B", "-m", this.layout.entryModule, "--json", "--resource-root", this.layout.resourceRoot,
      "--data-root", this.layout.dataRoot, "--frontend-port", "3120", "--api-port", "8120", command
    ]
    const child = this.spawnRuntime(this.layout.pythonPath, args, {
      cwd: this.layout.resourceRoot,
      env: buildPythonEnvironment(this.environment),
      windowsHide: true,
      shell: false,
      detached: true
    })
    if (!child.stdin) {
      child.kill("SIGTERM")
      throw new RuntimeInvocationError("runtime_stdin_unavailable", "Finalizer private stdin transport is unavailable", { command })
    }
    child.stdin.end(encoded)
    await new Promise<void>((resolve, reject) => {
      let buffer: Buffer<ArrayBufferLike> = Buffer.alloc(0)
      let stderr: Buffer<ArrayBufferLike> = Buffer.alloc(0)
      let settled = false
      const finish = (error?: RuntimeInvocationError) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        if (error) reject(error)
        else resolve()
      }
      const timer = setTimeout(() => {
        child.kill("SIGTERM")
        finish(new RuntimeInvocationError("runtime_finalizer_ready_timeout", "Finalizer did not complete its bounded ready handshake", { command, stderr: stderr.toString("utf8") }))
      }, FINALIZER_READY_TIMEOUT_MS)
      child.stderr.on("data", (chunk: Buffer | string) => { stderr = appendBounded(stderr, Buffer.from(chunk), STDERR_LIMIT).value })
      child.stdout.on("data", (chunk: Buffer | string) => {
        if (settled) return
        const next = appendBounded(buffer, Buffer.from(chunk), STDOUT_LIMIT)
        buffer = next.value
        if (next.overflow) {
          child.kill("SIGTERM")
          finish(new RuntimeInvocationError("runtime_stdout_limit", "Finalizer readiness output exceeded the protocol limit", { command, stderr: stderr.toString("utf8") }))
          return
        }
        const newline = buffer.indexOf(0x0a)
        if (newline < 0) return
        const line = buffer.subarray(0, newline).toString("utf8").trim()
        try {
          const ready = JSON.parse(line) as Record<string, unknown>
          if (ready.event !== "FINALIZER_READY" || ready.operationId !== operationId || ready.readyNonce !== readyNonce || Object.keys(ready).sort().join(",") !== "event,operationId,readyNonce") {
            throw new Error("mismatch")
          }
          // Readiness, not the terminal CLI JSON, is the host/finalizer handoff. The
          // detached child and both pipes must not keep Electron alive; host exit will
          // close its pipe ends and durable status/receipt remains authoritative.
          child.unref?.()
          const stdout = child.stdout as NodeJS.ReadableStream & { unref?(): void }
          const stderrStream = child.stderr as NodeJS.ReadableStream & { unref?(): void }
          stdout.unref?.()
          stderrStream.unref?.()
          finish()
        } catch {
          child.kill("SIGTERM")
          finish(new RuntimeInvocationError("runtime_finalizer_ready_invalid", "Finalizer returned an invalid ready handshake", { command, stderr: stderr.toString("utf8") }))
        }
      })
      child.on("error", error => finish(new RuntimeInvocationError("runtime_spawn_failed", `Finalizer process could not be started: ${redactText(error.message, 300)}`, { command, stderr: stderr.toString("utf8") })))
      child.on("close", exitCode => {
        if (!settled) finish(new RuntimeInvocationError("runtime_finalizer_exited_before_ready", "Finalizer exited before the ready handshake", { command, stderr: stderr.toString("utf8"), exitCode }))
      })
    })
  }
  private async invokeBounded(command: RuntimeCommand, options: RuntimeInvokeOptions, stdinPayload?: unknown): Promise<RuntimeCliResponse> {
    const args: string[] = [
      "-B",
      "-m",
      this.layout.entryModule,
      "--json",
      "--resource-root",
      this.layout.resourceRoot,
      "--data-root",
      this.layout.dataRoot,
      "--frontend-port",
      "3120",
      "--api-port",
      "8120",
      ...(command === "data-clear-preview" ? ["--timeout", "150"] : []),
      command
    ]
    if (command === "backup" && options.backupDestination) args.push("--destination", options.backupDestination)
    if (command === "restore") {
      if (!options.restoreBackupPath) throw new RuntimeInvocationError("runtime_restore_path_missing", "Restore requires a host-selected backup", { command })
      args.push("--backup-path", options.restoreBackupPath)
      if (options.activate) {
        args.push("--activate")
        if (options.personalMemoryConfirmation) args.push("--personal-memory-confirmation", options.personalMemoryConfirmation)
      }
    }
    if (command === "config-preview" || command === "config-apply") {
      if (options.configCandidate === undefined) throw new RuntimeInvocationError("runtime_config_candidate_missing", "Runtime configuration candidate is required", { command })
      args.push("--candidate-json", JSON.stringify(options.configCandidate))
    }
    if (command === "config-apply") {
      if (!Number.isSafeInteger(options.expectedRevision) || Number(options.expectedRevision) < 0 || typeof options.confirmationToken !== "string" || !/^desktop-config:[0-9a-f]{32}:[0-9a-f]{16}$/.test(options.confirmationToken)) {
        throw new RuntimeInvocationError("runtime_config_confirmation_missing", "Runtime configuration confirmation is required", { command })
      }
      args.push("--expected-revision", String(options.expectedRevision), "--confirmation-token", options.confirmationToken)
    }
    if (command === "credential-prompt") {
      if (typeof options.parentWindow !== "bigint" || options.parentWindow < 1n || options.parentWindow > 2n ** 63n - 1n) throw new RuntimeInvocationError("desktop_credential_parent_invalid", "A real native parent window is required", { command })
      args.push("--parent-window", options.parentWindow.toString())
    }
    if (command === "credential-delete") args.push("--confirmation-token", "delete-research-credential")
    const encodedStdin = stdinPayload === undefined ? null : Buffer.from(`${JSON.stringify(stdinPayload)}\n`, "utf8")
    if (encodedStdin && encodedStdin.length > STDIN_LIMIT) throw new RuntimeInvocationError("runtime_stdin_limit", "Runtime private stdin request exceeded the protocol limit", { command })
    const child = this.spawnRuntime(this.layout.pythonPath, args, {
      cwd: this.layout.resourceRoot,
      env: buildPythonEnvironment(this.environment),
      windowsHide: true,
      shell: false
    })
    if (encodedStdin && !child.stdin) {
      child.kill("SIGTERM")
      throw new RuntimeInvocationError("runtime_stdin_unavailable", "Runtime private stdin transport is unavailable", { command })
    }
    if (child.stdin) {
      if (encodedStdin) child.stdin.end(encodedStdin)
      else child.stdin.end()
    }

    return await new Promise<RuntimeCliResponse>((resolve, reject) => {
      let stdout: Buffer<ArrayBufferLike> = Buffer.alloc(0)
      let stderr: Buffer<ArrayBufferLike> = Buffer.alloc(0)
      let settled = false
      let overflowed = false
      let terminationError: RuntimeInvocationError | null = null
      let escalationTimer: NodeJS.Timeout | null = null
      let forceKillTimer: NodeJS.Timeout | null = null
      const timeoutMs = this.timing.commandTimeouts?.[command] ?? COMMAND_TIMEOUTS[command]
      const terminateGraceMs = this.timing.terminateGraceMs ?? TERMINATE_GRACE_MS
      const forceKillGraceMs = this.timing.forceKillGraceMs ?? FORCE_KILL_GRACE_MS

      const clearTimers = () => {
        clearTimeout(commandTimer)
        if (escalationTimer) clearTimeout(escalationTimer)
        if (forceKillTimer) clearTimeout(forceKillTimer)
      }
      const finishReject = (error: RuntimeInvocationError) => {
        if (settled) return
        settled = true
        clearTimers()
        reject(error)
      }
      const beginTermination = (error: RuntimeInvocationError) => {
        if (settled || terminationError) return
        terminationError = error
        clearTimeout(commandTimer)
        child.kill("SIGTERM")
        escalationTimer = setTimeout(() => {
          if (settled) return
          child.kill("SIGKILL")
          forceKillTimer = setTimeout(() => {
            if (settled || !terminationError) return
            finishReject(new RuntimeInvocationError(
              `${terminationError.code}_termination_unconfirmed`,
              `${terminationError.message}; child termination was not confirmed`,
              { command, stderr: stderr.toString("utf8") }
            ))
          }, forceKillGraceMs)
        }, terminateGraceMs)
      }
      const commandTimer = setTimeout(() => {
        beginTermination(new RuntimeInvocationError("runtime_timeout", `Runtime ${command} exceeded its finite deadline`, { command, stderr: stderr.toString("utf8") }))
      }, timeoutMs)

      child.stdout.on("data", (chunk: Buffer | string) => {
        const next = appendBounded(stdout, Buffer.from(chunk), STDOUT_LIMIT)
        stdout = next.value
        if (next.overflow && !overflowed) {
          overflowed = true
          beginTermination(new RuntimeInvocationError("runtime_stdout_limit", "Runtime stdout exceeded the protocol limit", { command, stderr: stderr.toString("utf8") }))
        }
      })
      child.stderr.on("data", (chunk: Buffer | string) => {
        stderr = appendBounded(stderr, Buffer.from(chunk), STDERR_LIMIT).value
      })
      child.on("error", error => {
        finishReject(new RuntimeInvocationError("runtime_spawn_failed", `Runtime process could not be started: ${redactText(error.message, 300)}`, { command, stderr: stderr.toString("utf8") }))
      })
      child.on("close", (exitCode: number | null) => {
        if (settled) return
        if (terminationError) {
          finishReject(new RuntimeInvocationError(terminationError.code, terminationError.message, { command, stderr: stderr.toString("utf8"), exitCode }))
          return
        }
        settled = true
        clearTimers()
        try {
          const response = parseRuntimeResponse(stdout.toString("utf8"), command)
          if (exitCode !== 0 && response.ok) {
            reject(new RuntimeInvocationError("runtime_exit_mismatch", "Runtime reported success with a non-zero exit code", { command, stderr: stderr.toString("utf8"), exitCode }))
            return
          }
          resolve(response)
        } catch (error) {
          if (error instanceof RuntimeInvocationError) {
            reject(new RuntimeInvocationError(error.code, error.message, { command, stderr: stderr.toString("utf8"), exitCode }))
          } else {
            reject(error)
          }
        }
      })
    })
  }
}









