import { spawn } from "node:child_process"
import { once } from "node:events"
import { cp, mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"

const scriptDir = path.dirname(fileURLToPath(import.meta.url))
const sourceFrontend = path.resolve(scriptDir, "..")
const canonicalOrigin = "http://127.0.0.1:3317"
const apiOrigin = "http://127.0.0.1:8317"
const frontendPort = "3317"
const buildTimeoutMs = 12 * 60 * 1000
const stopTimeoutMs = 4 * 1000
const workspacePrefix = "newsroom-paper-agent-e2e-"

const fixedEnvironment = {
  NEXT_TELEMETRY_DISABLED: "1",
  NEWSROOM_DEPLOYMENT_PROFILE: "local-desktop",
  NEWSROOM_FRONTEND_SURFACE: "portal",
  NEWSROOM_CANONICAL_ORIGIN: canonicalOrigin,
  NEWSROOM_DESKTOP_CANONICAL_ORIGIN: canonicalOrigin,
  NEWSROOM_PORTAL_ORIGIN: canonicalOrigin,
  NEWSROOM_API_BASE_URL: apiOrigin,
  NEXT_PUBLIC_NEWSROOM_API_BASE_URL: apiOrigin,
  NEXT_PUBLIC_API_BASE_URL: apiOrigin,
  NEWSROOM_DESKTOP_INSTANCE_KEY: `local-desktop:${canonicalOrigin}`,
  NEWSROOM_DESKTOP_RELEASE_ID: "paper-agent-e2e",
  NEWSROOM_DESKTOP_CONFIG_DIGEST: "e".repeat(64),
  NEWSROOM_DESKTOP_BOOT_ID: "e".repeat(32),
  TZ: "UTC",
}

for (const [name, expected] of Object.entries(fixedEnvironment)) {
  if (name === "TZ" || process.env[name] === undefined) continue
  if (process.env[name] !== expected) {
    throw new Error(`${name} must be ${JSON.stringify(expected)} for the paper-agent E2E profile`)
  }
}

const inheritedEnvironmentNames = [
  "CI",
  "CommonProgramFiles",
  "CommonProgramFiles(x86)",
  "CommonProgramW6432",
  "ComSpec",
  "FORCE_COLOR",
  "LANG",
  "LC_ALL",
  "NO_COLOR",
  "NUMBER_OF_PROCESSORS",
  "OS",
  "PATHEXT",
  "PATH",
  "PROCESSOR_ARCHITECTURE",
  "PROCESSOR_IDENTIFIER",
  "PROCESSOR_LEVEL",
  "PROCESSOR_REVISION",
  "ProgramFiles",
  "ProgramFiles(x86)",
  "ProgramW6432",
  "SystemDrive",
  "SystemRoot",
  "TEMP",
  "TMP",
  "USERDOMAIN",
  "USERNAME",
  "windir",
]

let isolatedRoot
let activeChild
let nextServer
let shuttingDown = false

for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.once(signal, () => {
    void shutdown(0)
  })
}

process.once("uncaughtException", error => {
  void shutdown(1, error.stack ?? error.message)
})
process.once("unhandledRejection", reason => {
  void shutdown(1, reason instanceof Error ? reason.stack ?? reason.message : String(reason))
})

try {
  isolatedRoot = await mkdtemp(path.join(os.tmpdir(), workspacePrefix))
  assertDisposableWorkspace(isolatedRoot)

  const isolatedFrontend = path.join(isolatedRoot, "frontend")
  await cp(sourceFrontend, isolatedFrontend, {
    recursive: true,
    dereference: false,
    filter: source => shouldCopySource(source),
  })

  await symlink(
    path.join(sourceFrontend, "node_modules"),
    path.join(isolatedFrontend, "node_modules"),
    process.platform === "win32" ? "junction" : "dir",
  )

  const isolatedPaperData = path.join(isolatedRoot, "e2e-data", "papers.json")
  await mkdir(path.dirname(isolatedPaperData), { recursive: true })
  await writeFile(isolatedPaperData, '{"papers":[]}\n', "utf8")

  const isolatedEnvironment = {
    ...fixedEnvironment,
    NEWSROOM_PAPERS_DATA_PATH: portablePath(isolatedPaperData),
  }
  const isolatedHome = path.join(isolatedRoot, "user-home")
  const isolatedAppData = path.join(isolatedHome, "AppData", "Roaming")
  const isolatedLocalAppData = path.join(isolatedHome, "AppData", "Local")
  await Promise.all([
    mkdir(isolatedAppData, { recursive: true }),
    mkdir(isolatedLocalAppData, { recursive: true }),
  ])
  const childEnvironment = sanitizedChildEnvironment({
    ...isolatedEnvironment,
    APPDATA: isolatedAppData,
    HOME: isolatedHome,
    LOCALAPPDATA: isolatedLocalAppData,
    USERPROFILE: isolatedHome,
  })
  await writeFile(
    path.join(isolatedRoot, ".env"),
    Object.entries(isolatedEnvironment)
      .map(([name, value]) => `${name}=${value}`)
      .join("\n") + "\n",
    { encoding: "utf8", mode: 0o600 },
  )

  const nextCli = path.join(sourceFrontend, "node_modules", "next", "dist", "bin", "next")
  await runBuild(nextCli, isolatedFrontend, childEnvironment)

  nextServer = spawn(
    process.execPath,
    [nextCli, "start", isolatedFrontend, "--hostname", "127.0.0.1", "--port", frontendPort],
    {
      cwd: isolatedFrontend,
      env: childEnvironment,
      stdio: "inherit",
      windowsHide: true,
      detached: false,
    },
  )
  activeChild = nextServer

  nextServer.once("error", error => {
    void shutdown(1, `Next production server failed to start: ${error.message}`)
  })
  nextServer.once("close", (code, signal) => {
    if (shuttingDown) return
    const detail = signal ? `signal ${signal}` : `exit code ${code ?? 1}`
    void shutdown(code ?? 1, `Next production server stopped unexpectedly with ${detail}`)
  })
} catch (error) {
  await shutdown(1, error instanceof Error ? error.stack ?? error.message : String(error))
}

function shouldCopySource(source) {
  const relative = path.relative(sourceFrontend, source)
  if (!relative) return true
  const topLevel = relative.split(path.sep)[0]
  if (new Set([".next", ".tmp", "data", "node_modules", "out", "test-results"]).has(topLevel)) return false
  const name = path.basename(source)
  if (name === "tsconfig.tsbuildinfo" || name.startsWith(".next-dev")) return false
  if (name === ".env" || name.startsWith(".env.")) return false
  return true
}

function sanitizedChildEnvironment(overrides) {
  const environment = {}
  for (const name of inheritedEnvironmentNames) {
    if (process.env[name] !== undefined) environment[name] = process.env[name]
  }
  return { ...environment, ...overrides }
}

async function runBuild(nextCli, isolatedFrontend, environment) {
  const build = spawn(
    process.execPath,
    [nextCli, "build", isolatedFrontend, "--webpack"],
    {
      cwd: isolatedFrontend,
      env: environment,
      stdio: "inherit",
      windowsHide: true,
      detached: false,
    },
  )
  activeChild = build

  let timedOut = false
  const timeout = setTimeout(() => {
    timedOut = true
    void terminateChild(build)
  }, buildTimeoutMs)
  timeout.unref()

  const [code, signal] = await once(build, "close")
  clearTimeout(timeout)
  if (activeChild === build) activeChild = undefined
  if (timedOut) throw new Error(`Next production build exceeded ${buildTimeoutMs} ms`)
  if (code !== 0) {
    const detail = signal ? `signal ${signal}` : `exit code ${code ?? 1}`
    throw new Error(`Next production build failed with ${detail}`)
  }
}

async function shutdown(exitCode, message) {
  if (shuttingDown) return
  shuttingDown = true
  if (message) console.error(message)

  let shutdownFailed = false
  try {
    await terminateChild(activeChild ?? nextServer)
  } catch (error) {
    shutdownFailed = true
    console.error(error instanceof Error ? error.stack ?? error.message : String(error))
  }
  activeChild = undefined

  try {
    await cleanupWorkspace()
  } catch (error) {
    shutdownFailed = true
    console.error(error instanceof Error ? error.stack ?? error.message : String(error))
  }

  process.exit(exitCode === 0 && shutdownFailed ? 1 : exitCode)
}

async function terminateChild(child) {
  if (!child?.pid || child.exitCode !== null || child.signalCode !== null) return

  const closed = once(child, "close").then(() => true)
  child.kill("SIGTERM")
  if (await Promise.race([closed, delay(stopTimeoutMs).then(() => false)])) return

  if (process.platform === "win32") {
    const killer = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
      stdio: "ignore",
      windowsHide: true,
      detached: false,
    })
    await Promise.race([once(killer, "close"), delay(stopTimeoutMs)])
  } else {
    child.kill("SIGKILL")
  }
  await Promise.race([closed, delay(stopTimeoutMs)])
}

async function cleanupWorkspace() {
  if (!isolatedRoot) return
  assertDisposableWorkspace(isolatedRoot)
  await rm(isolatedRoot, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 })
  isolatedRoot = undefined
}

function assertDisposableWorkspace(candidate) {
  const resolvedTemp = path.resolve(os.tmpdir())
  const resolvedCandidate = path.resolve(candidate)
  if (
    path.dirname(resolvedCandidate) !== resolvedTemp ||
    !path.basename(resolvedCandidate).startsWith(workspacePrefix)
  ) {
    throw new Error(`Refusing to clean unexpected E2E workspace: ${resolvedCandidate}`)
  }
}

function portablePath(value) {
  return process.platform === "win32" ? value.replaceAll("\\", "/") : value
}

function delay(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds))
}
