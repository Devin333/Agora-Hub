import { spawn, type ChildProcess } from "node:child_process"
import { existsSync } from "node:fs"
import path from "node:path"
import type { ConnectionFailureCode } from "./load-failure"

/**
 * The Docker command line, as the local stack uses it.
 *
 * A desktop app launched from the Dock or the Start menu does not inherit the
 * user's shell PATH, so the executable is looked up in the places Docker
 * Desktop and the common package managers install it before PATH. Commands get
 * only the environment Docker itself needs: a user's shell often exports model
 * API keys, and none of them may reach a container. Every command has a
 * deadline; when it passes, or the caller aborts, the whole process tree is
 * ended so no half-finished pull keeps running in the background.
 */
export interface DockerCommandOptions {
  cwd?: string
  /** Written to stdin, then stdin is closed. Never put secrets in arguments. */
  input?: string
  timeoutMs: number
  signal?: AbortSignal
  /** Each complete line of combined output, as it arrives. */
  onLine?(line: string): void
}

export interface DockerCommandResult {
  code: number | null
  stdout: string
  stderr: string
  timedOut: boolean
  aborted: boolean
}

export type DockerRunner = (args: readonly string[], options: DockerCommandOptions) => Promise<DockerCommandResult>

export interface DockerLocatorEnvironment {
  platform: NodeJS.Platform
  env: NodeJS.ProcessEnv
  exists(filePath: string): boolean
}

const MAX_CAPTURE = 256 * 1024

/** Variables Docker and its credential helpers need; everything else stays out. */
const PASSED_VARIABLES = [
  "HOME", "USER", "LOGNAME", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
  "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "ProgramData",
  "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "SystemRoot", "SYSTEMROOT", "windir", "SystemDrive",
  "COMSPEC", "PATHEXT", "TEMP", "TMP",
  "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY"
]

function candidateExecutables(environment: DockerLocatorEnvironment): string[] {
  const { platform, env } = environment
  if (platform === "win32") {
    const programFiles = env.ProgramW6432 || env.ProgramFiles || "C:\\Program Files"
    return [path.win32.join(programFiles, "Docker", "Docker", "resources", "bin", "docker.exe")]
  }
  if (platform === "darwin") {
    return [
      "/usr/local/bin/docker",
      "/opt/homebrew/bin/docker",
      "/Applications/Docker.app/Contents/Resources/bin/docker",
      ...(env.HOME ? [path.posix.join(env.HOME, ".docker", "bin", "docker"), path.posix.join(env.HOME, ".orbstack", "bin", "docker")] : [])
    ]
  }
  return ["/usr/bin/docker", "/usr/local/bin/docker", "/snap/bin/docker"]
}

function pathEntries(environment: DockerLocatorEnvironment): string[] {
  const raw = environment.env.PATH ?? environment.env.Path ?? ""
  const separator = environment.platform === "win32" ? ";" : ":"
  return raw.split(separator).filter(Boolean)
}

/** The Docker executable to run, or null when none is installed. */
export function locateDocker(environment: DockerLocatorEnvironment): string | null {
  for (const candidate of candidateExecutables(environment)) {
    if (environment.exists(candidate)) return candidate
  }
  const join = environment.platform === "win32" ? path.win32.join : path.posix.join
  const name = environment.platform === "win32" ? "docker.exe" : "docker"
  for (const directory of pathEntries(environment)) {
    const candidate = join(directory, name)
    if (environment.exists(candidate)) return candidate
  }
  return null
}

/**
 * The environment a Docker command runs with. The executable's own directory
 * leads PATH so Docker Desktop finds its credential helpers next to it.
 */
export function dockerEnvironment(executable: string, environment: DockerLocatorEnvironment): NodeJS.ProcessEnv {
  const result: NodeJS.ProcessEnv = {}
  for (const name of PASSED_VARIABLES) {
    const value = environment.env[name]
    if (value !== undefined) result[name] = value
  }
  const separator = environment.platform === "win32" ? ";" : ":"
  const directory = environment.platform === "win32" ? path.win32.dirname(executable) : path.posix.dirname(executable)
  const fallback = environment.platform === "win32" ? [] : ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
  const entries = [directory, ...pathEntries(environment), ...fallback]
  result.PATH = [...new Set(entries)].join(separator)
  // Plain, uncoloured output that the stack can read line by line.
  result.DOCKER_CLI_HINTS = "false"
  result.COMPOSE_ANSI = "never"
  result.NO_COLOR = "1"
  return result
}

function killTree(child: ChildProcess, platform: NodeJS.Platform): void {
  if (child.exitCode !== null || child.pid === undefined) return
  if (platform === "win32") {
    // Docker Compose starts helper processes; only taskkill ends all of them.
    spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], { windowsHide: true, stdio: "ignore" }).on("error", () => child.kill())
    return
  }
  try {
    process.kill(-child.pid, "SIGKILL")
  } catch {
    child.kill("SIGKILL")
  }
}

function appendCapped(current: string, chunk: string): string {
  const next = current + chunk
  return next.length <= MAX_CAPTURE ? next : next.slice(next.length - MAX_CAPTURE)
}

export function createDockerRunner(executable: string, environment: DockerLocatorEnvironment): DockerRunner {
  const env = dockerEnvironment(executable, environment)
  return (args, options) => new Promise(resolve => {
    let stdout = ""
    let stderr = ""
    let timedOut = false
    let aborted = false
    let settled = false
    const child = spawn(executable, [...args], {
      cwd: options.cwd,
      env,
      windowsHide: true,
      // Its own process group on POSIX, so the whole tree can be ended at once.
      detached: environment.platform !== "win32",
      stdio: ["pipe", "pipe", "pipe"]
    })
    const pending = { stdout: "", stderr: "" }
    const consume = (stream: "stdout" | "stderr", chunk: Buffer) => {
      const text = chunk.toString("utf8")
      if (stream === "stdout") stdout = appendCapped(stdout, text)
      else stderr = appendCapped(stderr, text)
      if (!options.onLine) return
      const lines = (pending[stream] + text).split(/\r?\n|\r/)
      pending[stream] = lines.pop() ?? ""
      for (const line of lines) if (line.trim()) options.onLine(line)
    }
    child.stdout?.on("data", chunk => consume("stdout", chunk))
    child.stderr?.on("data", chunk => consume("stderr", chunk))
    const finish = (code: number | null, error?: Error) => {
      if (settled) return
      settled = true
      clearTimeout(deadline)
      options.signal?.removeEventListener("abort", onAbort)
      for (const stream of ["stdout", "stderr"] as const) {
        if (pending[stream].trim()) options.onLine?.(pending[stream])
      }
      if (error) stderr = appendCapped(stderr, `${error.message}\n`)
      resolve({ code, stdout, stderr, timedOut, aborted })
    }
    const deadline = setTimeout(() => {
      timedOut = true
      killTree(child, environment.platform)
    }, options.timeoutMs)
    const onAbort = () => {
      aborted = true
      killTree(child, environment.platform)
    }
    if (options.signal?.aborted) onAbort()
    else options.signal?.addEventListener("abort", onAbort, { once: true })
    child.once("error", error => finish(null, error))
    child.once("close", code => finish(code))
    child.stdin?.on("error", () => undefined)
    child.stdin?.end(options.input ?? "")
  })
}

export function systemDockerEnvironment(): DockerLocatorEnvironment {
  return { platform: process.platform, env: process.env, exists: existsSync }
}

/** The one failure category a failed Docker command's output points to. */
export function classifyDockerFailure(output: string): ConnectionFailureCode {
  const text = output.toLowerCase()
  if (/no space left on device|not enough space on the disk/.test(text)) return "disk_full"
  if (/permission denied.*docker|docker.*permission denied|access is denied/.test(text)) return "docker_permission"
  if (/cannot connect to the docker daemon|error during connect|is the docker daemon running|docker desktop is not running|the system cannot find the file specified.*pipe|docker_engine|dockerdesktoplinuxengine/.test(text)) return "docker_not_running"
  if (/port is already allocated|address already in use|bind: an attempt was made to access a socket|only one usage of each socket address/.test(text)) return "local_port_in_use"
  if (/manifest unknown|manifest for .* not found|not found: manifest|pull access denied|repository does not exist|denied: |unauthorized/.test(text)) return "image_unavailable"
  if (/tls handshake timeout|i\/o timeout|dial tcp|no such host|connection reset|connection refused|net\/http|context deadline exceeded|\beof\b|temporary failure in name resolution|toomanyrequests/.test(text)) return "image_pull_failed"
  return "local_stack_failed"
}
