import { PassThrough } from "node:stream"
import { EventEmitter } from "node:events"
import { describe, expect, it, vi } from "vitest"
import { COMMAND_TIMEOUTS, RuntimeClient, RuntimeInvocationError, buildPythonEnvironment, parseRuntimeResponse, type SpawnedProcess } from "../src/runtime-client"
import type { RuntimeLayout } from "../src/runtime-layout"

class FakeChild extends EventEmitter implements SpawnedProcess {
  stdin = new PassThrough()
  stdout = Object.assign(new PassThrough(), { unref: vi.fn() })
  stderr = Object.assign(new PassThrough(), { unref: vi.fn() })
  kill = vi.fn(() => true)
  unref = vi.fn()
}

const layout: RuntimeLayout = {
  resourceRoot: "C:\\Agora\\runtime",
  dataRoot: "C:\\Users\\tester\\AppData\\Roaming\\Agora\\runtime",
  pythonPath: "C:\\Agora\\runtime\\.venv\\Scripts\\python.exe",
  manifestPath: "C:\\Agora\\runtime\\runtime-manifest.json",
  entryModule: "scripts.desktop_app",
  mode: "packaged"
}

function response(command = "status") {
  return JSON.stringify({ ok: true, command, state: "READY", code: "runtime_ready", message: "ready", data: { canonical_origin: "http://127.0.0.1:3120", instance_key: "owned" } })
}

describe("parseRuntimeResponse", () => {
  it("accepts only the new bounded desktop protocol", () => {
    expect(parseRuntimeResponse(response(), "status").data.instance_key).toBe("owned")
    expect(() => parseRuntimeResponse(JSON.stringify({ ok: true, action: "status", status: "READY", reason_code: "ok", details: {} }), "status")).toThrowError(RuntimeInvocationError)
    expect(() => parseRuntimeResponse(response("start"), "status")).toThrowError(/does not match/)
  })

  it("bounds protocol metadata before it can reach renderer logs", () => {
    const payload = JSON.parse(response())
    payload.message = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
    expect(parseRuntimeResponse(JSON.stringify(payload), "status").message).not.toContain("abcdefghijklmnopqrstuvwxyz")
    payload.code = "runtime_ready\nsecret"
    expect(() => parseRuntimeResponse(JSON.stringify(payload), "status")).toThrowError(/metadata/)
  })
})

describe("RuntimeClient", () => {
  it("isolates every Python child from user-site, PYTHONHOME, and PYTHONPATH", () => {
    const environment = buildPythonEnvironment({ Path: "C:\\Windows", PYTHONHOME: "C:\\Users\\Alice\\Python", PythonPath: "C:\\Users\\Alice\\AppData\\Roaming\\Python", SAFE: "yes" })
    expect(environment).toEqual({ Path: "C:\\Windows", SAFE: "yes", PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1", PYTHONUNBUFFERED: "1" })
    expect(Object.keys(environment).map(key => key.toUpperCase())).not.toContain("PYTHONHOME")
    expect(Object.keys(environment).map(key => key.toUpperCase())).not.toContain("PYTHONPATH")
  })

  it("uses the owned interpreter, exact module arguments, no shell, and parses stdout", async () => {
    const child = new FakeChild()
    const spawn = vi.fn(() => child)
    const client = new RuntimeClient(layout, spawn, { PATH: "ignored" })
    const pending = client.invoke("status")
    child.stdout.end(response())
    child.emit("close", 0)
    await expect(pending).resolves.toMatchObject({ ok: true, command: "status" })
    expect(spawn).toHaveBeenCalledWith(layout.pythonPath, [
      "-B", "-m", "scripts.desktop_app", "--json", "--resource-root", layout.resourceRoot,
      "--data-root", layout.dataRoot, "--frontend-port", "3120", "--api-port", "8120", "status"
    ], expect.objectContaining({ cwd: layout.resourceRoot, windowsHide: true, shell: false, env: { PATH: "ignored", PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1", PYTHONUNBUFFERED: "1" } }))
  })

  it("gives clear preview a realistic bounded owner drain timeout", async () => {
    const child = new FakeChild()
    const spawn = vi.fn(() => child)
    const client = new RuntimeClient(layout, spawn)
    const pending = client.invoke("data-clear-preview")
    child.stdout.end(response("data-clear-preview"))
    child.emit("close", 0)
    await pending
    const args = (spawn.mock.calls[0] as unknown as [string, string[]])[1]
    expect(args.slice(-3)).toEqual(["--timeout", "150", "data-clear-preview"])
    expect(COMMAND_TIMEOUTS["data-clear-preview"]).toBe(5 * 60_000)
    expect(COMMAND_TIMEOUTS["data-clear-apply"]).toBe(5 * 60_000)
  })
  it("keeps private clear capabilities on bounded stdin and out of argv", async () => {
    const child = new FakeChild()
    const spawn = vi.fn(() => child)
    const chunks: Buffer[] = []
    child.stdin.on("data", (chunk: Buffer | string) => chunks.push(Buffer.from(chunk)))
    const client = new RuntimeClient(layout, spawn)
    const pending = client.invokePrivate("data-clear-apply", { preview_capability: "private-value", parent_pid: 1234 })
    child.stdout.end(response("data-clear-apply"))
    child.emit("close", 0)
    await expect(pending).resolves.toMatchObject({ command: "data-clear-apply" })
    const args = (spawn.mock.calls[0] as unknown as [string, string[]])[1]
    expect(args).toContain("data-clear-apply")
    expect(args.join(" ")).not.toContain("private-value")
    expect(Buffer.concat(chunks).toString("utf8")).toBe('{"preview_capability":"private-value","parent_pid":1234}\n')
  })

  it("launches only the fixed finalizer and requires the exact ready nonce before host exit", async () => {
    const child = new FakeChild()
    const spawn = vi.fn(() => child)
    const chunks: Buffer[] = []
    child.stdin.on("data", (chunk: Buffer | string) => chunks.push(Buffer.from(chunk)))
    const client = new RuntimeClient(layout, spawn)
    const request = {
      version: 1,
      operationId: "d".repeat(32),
      finalizerCapability: "private-finalizer-capability",
      electron: { pid: 1234, creationTime: "133801234567890000" },
      readyNonce: "ready_nonce_12345678901234567890"
    }
    const pending = client.launchDataClearFinalizer(request)
    child.stdout.write(`${JSON.stringify({ event: "FINALIZER_READY", operationId: request.operationId, readyNonce: request.readyNonce })}\n`)
    await expect(pending).resolves.toBeUndefined()
    const call = spawn.mock.calls[0] as unknown as [string, string[], Record<string, unknown>]
    expect(call[1].at(-1)).toBe("data-clear-finalize")
    expect(call[1].join(" ")).not.toContain(request.finalizerCapability)
    expect(call[2]).toMatchObject({ detached: true, shell: false, windowsHide: true })
    expect(Buffer.concat(chunks).toString("utf8")).toBe(`${JSON.stringify(request)}\n`)
    expect(child.unref).toHaveBeenCalledOnce()
    expect(child.stdout.unref).toHaveBeenCalledOnce()
    expect(child.stderr.unref).toHaveBeenCalledOnce()
    expect(child.listenerCount("close")).toBeGreaterThan(0)
  })
  it("rejects private clear commands on the public argv transport", async () => {
    const client = new RuntimeClient(layout, vi.fn(() => new FakeChild()))
    await expect(client.invoke("data-clear-apply")).rejects.toMatchObject({ code: "runtime_stdin_required" })
  })
  it("passes only host-selected restore identity and the bound personal-memory confirmation to the adapter", async () => {
    const child = new FakeChild()
    const spawn = vi.fn(() => child)
    const client = new RuntimeClient(layout, spawn)
    const pending = client.invoke("restore", {
      restoreBackupPath: "C:\\Backups\\paper.zip",
      activate: true,
      personalMemoryConfirmation: "restore-personal-memory:backup-1"
    })
    child.stdout.end(response("restore"))
    child.emit("close", 0)
    await expect(pending).resolves.toMatchObject({ command: "restore" })
    expect(spawn).toHaveBeenCalledWith(layout.pythonPath, [
      "-B", "-m", "scripts.desktop_app", "--json", "--resource-root", layout.resourceRoot,
      "--data-root", layout.dataRoot, "--frontend-port", "3120", "--api-port", "8120", "restore",
      "--backup-path", "C:\\Backups\\paper.zip", "--activate", "--personal-memory-confirmation", "restore-personal-memory:backup-1"
    ], expect.objectContaining({ shell: false, windowsHide: true }))
  })

  it("passes only host-held config CAS material and preserves the finite UNKNOWN timeout envelope", async () => {
    const child = new FakeChild()
    const spawn = vi.fn(() => child)
    const client = new RuntimeClient(layout, spawn, { NEWSROOM_DESKTOP_RESEARCH_API_KEY: "must-not-inherit" })
    const candidate = {
      llm: { route_id: "writer-primary", base_url: "https://api.example.test/v1", model: "research-model" },
      source: { api_url: "https://export.arxiv.org/api/query", timeout_seconds: 30 }
    }
    const token = `desktop-config:${"a".repeat(32)}:${"b".repeat(16)}`
    const pending = client.invoke("config-apply", { configCandidate: candidate, expectedRevision: 7, confirmationToken: token })
    child.stdout.end(response("config-apply"))
    child.emit("close", 0)
    await expect(pending).resolves.toMatchObject({ command: "config-apply" })
    const call = spawn.mock.calls[0] as unknown as Parameters<import("../src/runtime-client").SpawnRuntime>
    const args = call[1]
    expect(args).toEqual([
      "-B", "-m", "scripts.desktop_app", "--json", "--resource-root", layout.resourceRoot,
      "--data-root", layout.dataRoot, "--frontend-port", "3120", "--api-port", "8120", "config-apply",
      "--candidate-json", JSON.stringify(candidate), "--expected-revision", "7", "--confirmation-token", token
    ])
    expect(call[2].env).not.toHaveProperty("NEWSROOM_DESKTOP_RESEARCH_API_KEY")
    expect(COMMAND_TIMEOUTS["config-apply"]).toBe(900_000)
  })

  it("rejects malformed config receipts before spawning Python", async () => {
    const spawn = vi.fn()
    const client = new RuntimeClient(layout, spawn)
    await expect(client.invoke("config-apply", { configCandidate: {}, expectedRevision: 0, confirmationToken: "renderer-token" })).rejects.toMatchObject({ code: "runtime_config_confirmation_missing" })
    expect(spawn).not.toHaveBeenCalled()
  })

  it("rejects oversized stdout instead of parsing a truncated success", async () => {
    const child = new FakeChild()
    const client = new RuntimeClient(layout, () => child)
    const pending = client.invoke("status")
    child.stdout.write(Buffer.alloc(300 * 1024, 65))
    child.emit("close", null)
    await expect(pending).rejects.toMatchObject({ code: "runtime_stdout_limit" })
    expect(child.kill).toHaveBeenCalled()
  })

  it("keeps ownership of a timed-out child until termination is confirmed", async () => {
    vi.useFakeTimers()
    try {
      const child = new FakeChild()
      const client = new RuntimeClient(layout, () => child, {}, {
        commandTimeouts: { status: 100 },
        terminateGraceMs: 50,
        forceKillGraceMs: 50
      })
      const pending = client.invoke("status")
      const rejection = expect(pending).rejects.toMatchObject({ code: "runtime_timeout" })
      await vi.advanceTimersByTimeAsync(101)
      expect(child.kill).toHaveBeenCalledWith("SIGTERM")
      let resolved = false
      void pending.catch(() => { resolved = true })
      await vi.advanceTimersByTimeAsync(20)
      expect(resolved).toBe(false)
      child.emit("close", null)
      await rejection
    } finally {
      vi.useRealTimers()
    }
  })

  it("escalates termination finitely and reports when child exit cannot be confirmed", async () => {
    vi.useFakeTimers()
    try {
      const child = new FakeChild()
      const client = new RuntimeClient(layout, () => child, {}, {
        commandTimeouts: { status: 100 },
        terminateGraceMs: 50,
        forceKillGraceMs: 50
      })
      const pending = client.invoke("status")
      const rejection = expect(pending).rejects.toMatchObject({ code: "runtime_timeout_termination_unconfirmed" })
      await vi.advanceTimersByTimeAsync(201)
      expect(child.kill.mock.calls).toEqual([["SIGTERM"], ["SIGKILL"]])
      await rejection
    } finally {
      vi.useRealTimers()
    }
  })

  it("redacts bounded stderr in transport failures", async () => {
    const child = new FakeChild()
    const client = new RuntimeClient(layout, () => child)
    const pending = client.invoke("status")
    child.stderr.write("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    child.stdout.end("not-json")
    child.emit("close", 2)
    await expect(pending).rejects.toMatchObject({ code: "runtime_protocol_invalid_json", stderr: expect.not.stringContaining("abcdefghijklmnopqrstuvwxyz") })
  })
})








