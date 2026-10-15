import assert from "node:assert/strict"
import { EventEmitter } from "node:events"
import test from "node:test"
import {
  ProcessDeadlineError,
  bindShutdownSignals,
  createIdempotentShutdown,
  createOwnedProcessLifecycle,
  waitForProcessClose,
} from "./paper-agent-e2e-lifecycle.mjs"

class FakeChild extends EventEmitter {
  constructor(pid) {
    super()
    this.pid = pid
    this.exitCode = null
    this.signalCode = null
    this.killCalls = []
  }

  kill(signal) {
    this.killCalls.push(signal)
    return true
  }

  close(code, signal = null) {
    this.exitCode = code
    this.signalCode = signal
    this.emit("close", code, signal)
  }
}

function manualTimers() {
  let nextId = 1
  const pending = new Map()

  return {
    setTimeout(callback, milliseconds) {
      const handle = {
        id: nextId,
        milliseconds,
        unref() {},
      }
      nextId += 1
      pending.set(handle.id, { handle, callback })
      return handle
    },
    clearTimeout(handle) {
      if (handle) pending.delete(handle.id)
    },
    fireNext() {
      const entry = pending.values().next().value
      assert.ok(entry, "expected a pending timer")
      pending.delete(entry.handle.id)
      entry.callback()
    },
    count() {
      return pending.size
    },
  }
}

test("waitForProcessClose returns a successful build result", async () => {
  const timers = manualTimers()
  const build = new FakeChild(101)
  const resultPromise = waitForProcessClose(build, {
    deadlineMs: 100,
    label: "build",
    timers,
  })

  build.close(0)

  assert.deepEqual(await resultPromise, { code: 0, signal: null })
  assert.equal(timers.count(), 0)
})

test("waitForProcessClose preserves a failed build exit", async () => {
  const timers = manualTimers()
  const build = new FakeChild(102)
  const resultPromise = waitForProcessClose(build, {
    deadlineMs: 100,
    label: "build",
    timers,
  })

  build.close(2)

  assert.deepEqual(await resultPromise, { code: 2, signal: null })
})

test("waitForProcessClose rejects at the strict deadline when close never arrives", async () => {
  const timers = manualTimers()
  const build = new FakeChild(103)
  const resultPromise = waitForProcessClose(build, {
    deadlineMs: 25,
    label: "build without close",
    timers,
  })

  timers.fireNext()

  await assert.rejects(
    resultPromise,
    error => error instanceof ProcessDeadlineError && error.deadlineMs === 25,
  )
  assert.equal(timers.count(), 0)
})

test("Windows termination invokes taskkill for the complete owned PID tree even after parent close", async () => {
  const timers = manualTimers()
  const parent = new FakeChild(4242)
  parent.exitCode = 0
  const calls = []
  let killer
  const lifecycle = createOwnedProcessLifecycle({
    platform: "win32",
    timers,
    logger: { info() {} },
    spawnProcess(command, args, options) {
      calls.push({ command, args, options })
      killer = new FakeChild(5252)
      return killer
    },
  })
  lifecycle.own(parent, "closed build parent")

  const termination = lifecycle.terminateTree(parent, { deadlineMs: 50 })
  killer.close(0)
  await termination

  assert.deepEqual(calls, [{
    command: "taskkill",
    args: ["/PID", "4242", "/T", "/F"],
    options: { stdio: "ignore", windowsHide: true, detached: false },
  }])
  assert.deepEqual(parent.killCalls, [])
})

test("build deadline triggers bounded Windows tree termination and shutdown cleanup", { timeout: 1000 }, async () => {
  const timers = manualTimers()
  const build = new FakeChild(6060)
  let killer
  let resolveKillerSpawned
  const killerSpawned = new Promise(resolve => { resolveKillerSpawned = resolve })
  const lifecycle = createOwnedProcessLifecycle({
    platform: "win32",
    timers,
    logger: { info() {} },
    spawnProcess() {
      killer = new FakeChild(6161)
      resolveKillerSpawned()
      return killer
    },
  })
  lifecycle.own(build, "Next production build")

  const calls = []
  const shutdown = createIdempotentShutdown({
    terminate: () => lifecycle.terminateOwnedTrees({ deadlineMs: 50 }),
    cleanup: async () => { calls.push("cleanup") },
    exit: async code => { calls.push(["exit", code]) },
    logger: { error() {} },
  })
  const buildResult = lifecycle.waitForExit(build, { deadlineMs: 25 })
    .catch(async error => {
      await shutdown(1, error.message)
      throw error
    })

  timers.fireNext()
  await killerSpawned
  assert.ok(killer, "deadline shutdown should spawn taskkill")
  killer.close(0)

  await assert.rejects(buildResult, ProcessDeadlineError)
  assert.deepEqual(calls, ["cleanup", ["exit", 1]])
})

test("signal-driven shutdown is idempotent", async () => {
  const processTarget = new EventEmitter()
  const calls = []
  let resolveExit
  const exited = new Promise(resolve => { resolveExit = resolve })
  const shutdown = createIdempotentShutdown({
    terminate: async () => { calls.push("terminate") },
    cleanup: async () => { calls.push("cleanup") },
    exit: async code => {
      calls.push(["exit", code])
      resolveExit()
    },
    logger: { error() {} },
  })
  const unbind = bindShutdownSignals({ processTarget, shutdown })

  processTarget.emit("SIGTERM")
  processTarget.emit("SIGINT")
  await exited
  unbind()

  assert.deepEqual(calls, ["terminate", "cleanup", ["exit", 0]])
})
