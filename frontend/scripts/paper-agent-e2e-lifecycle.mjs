export class ProcessDeadlineError extends Error {
  constructor(label, deadlineMs) {
    super(`${label} did not close within ${deadlineMs} ms`)
    this.name = "ProcessDeadlineError"
    this.code = "PROCESS_DEADLINE_EXCEEDED"
    this.label = label
    this.deadlineMs = deadlineMs
  }
}

export class ProcessTerminationError extends Error {
  constructor(label, code, signal) {
    const detail = signal ? `signal ${signal}` : `exit code ${code ?? "unknown"}`
    super(`${label} termination command failed with ${detail}`)
    this.name = "ProcessTerminationError"
    this.code = "PROCESS_TERMINATION_FAILED"
    this.label = label
    this.exitCode = code
    this.signal = signal
  }
}

export function waitForProcessClose(
  child,
  {
    deadlineMs,
    label = "owned process",
    timers = globalThis,
  },
) {
  if (!Number.isFinite(deadlineMs) || deadlineMs <= 0) {
    throw new TypeError("deadlineMs must be a positive finite number")
  }

  let timeout
  let onClose
  let onError

  const close = new Promise((resolve, reject) => {
    onClose = (code, signal) => resolve({ code, signal })
    onError = error => reject(error)
    child.once("close", onClose)
    child.once("error", onError)
  })
  const deadline = new Promise((_, reject) => {
    timeout = timers.setTimeout(() => {
      reject(new ProcessDeadlineError(label, deadlineMs))
    }, deadlineMs)
    timeout?.unref?.()
  })

  return Promise.race([close, deadline]).finally(() => {
    timers.clearTimeout(timeout)
    child.removeListener("close", onClose)
    child.removeListener("error", onError)
  })
}

export function createOwnedProcessLifecycle({
  platform = process.platform,
  spawnProcess,
  timers = globalThis,
  logger = console,
} = {}) {
  if (typeof spawnProcess !== "function") {
    throw new TypeError("spawnProcess is required")
  }

  const owned = new Map()
  const terminationPromises = new WeakMap()

  function own(child, label) {
    if (!child || typeof child.once !== "function") {
      throw new TypeError("owned child must be a ChildProcess-compatible object")
    }
    owned.set(child, label)
    return child
  }

  function release(child) {
    owned.delete(child)
  }

  function waitForExit(child, { deadlineMs, label = owned.get(child) ?? "owned process" }) {
    return waitForProcessClose(child, { deadlineMs, label, timers })
  }

  function terminateTree(child, { deadlineMs, label = owned.get(child) ?? "owned process" }) {
    const existing = terminationPromises.get(child)
    if (existing) return existing

    const termination = platform === "win32"
      ? terminateWindowsTree(child, { deadlineMs, label })
      : terminatePosixTree(child, { deadlineMs, label })
    terminationPromises.set(child, termination)
    return termination
  }

  async function terminateOwnedTrees({ deadlineMs }) {
    const entries = [...owned.entries()]
    const results = await Promise.allSettled(
      entries.map(([child, label]) => terminateTree(child, { deadlineMs, label })),
    )
    const failures = results
      .filter(result => result.status === "rejected")
      .map(result => result.reason)
    if (failures.length) {
      throw new AggregateError(failures, "one or more owned process trees could not be terminated")
    }
  }

  async function terminateWindowsTree(child, { deadlineMs, label }) {
    const pid = requireOwnedPid(child, label)
    logger.info?.(`Terminating Windows process tree for ${label} (PID ${pid})`)
    const killer = spawnProcess(
      "taskkill",
      ["/PID", String(pid), "/T", "/F"],
      {
        stdio: "ignore",
        windowsHide: true,
        detached: false,
      },
    )
    const result = await waitForProcessClose(killer, {
      deadlineMs,
      label: `taskkill for ${label}`,
      timers,
    })
    if (result.code !== 0) {
      throw new ProcessTerminationError(label, result.code, result.signal)
    }
  }

  async function terminatePosixTree(child, { deadlineMs, label }) {
    child.kill("SIGTERM")
    try {
      await waitForProcessClose(child, { deadlineMs, label, timers })
      return
    } catch (error) {
      if (!(error instanceof ProcessDeadlineError)) throw error
    }

    child.kill("SIGKILL")
    await waitForProcessClose(child, {
      deadlineMs,
      label: `${label} after SIGKILL`,
      timers,
    })
  }

  return {
    own,
    release,
    waitForExit,
    terminateTree,
    terminateOwnedTrees,
  }
}

export function bindShutdownSignals({
  processTarget = process,
  shutdown,
  signals = ["SIGINT", "SIGTERM", "SIGHUP"],
}) {
  if (typeof shutdown !== "function") throw new TypeError("shutdown is required")

  const bindings = signals.map(signal => {
    const handler = () => {
      void shutdown(0)
    }
    processTarget.once(signal, handler)
    return [signal, handler]
  })

  return function unbindShutdownSignals() {
    for (const [signal, handler] of bindings) {
      processTarget.removeListener(signal, handler)
    }
  }
}

export function createIdempotentShutdown({
  terminate,
  cleanup,
  exit,
  logger = console,
}) {
  if (typeof terminate !== "function" || typeof cleanup !== "function" || typeof exit !== "function") {
    throw new TypeError("terminate, cleanup, and exit are required")
  }

  let shutdownPromise

  return function shutdown(exitCode, message) {
    if (shutdownPromise) return shutdownPromise

    shutdownPromise = Promise.resolve().then(async () => {
      if (message) logger.error?.(message)
      let failed = false

      try {
        await terminate()
      } catch (error) {
        failed = true
        logger.error?.(formatError(error))
      }

      try {
        await cleanup()
      } catch (error) {
        failed = true
        logger.error?.(formatError(error))
      }

      const finalCode = exitCode === 0 && failed ? 1 : exitCode
      await exit(finalCode)
      return finalCode
    })

    return shutdownPromise
  }
}

function requireOwnedPid(child, label) {
  if (!Number.isSafeInteger(child?.pid) || child.pid <= 0) {
    throw new TypeError(`${label} does not have a valid owned PID`)
  }
  return child.pid
}

function formatError(error) {
  return error instanceof Error ? error.stack ?? error.message : String(error)
}
