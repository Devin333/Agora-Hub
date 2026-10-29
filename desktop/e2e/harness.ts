import { spawn } from "node:child_process"
import { createRequire } from "node:module"
import path from "node:path"
import { _electron as electron, type ElectronApplication, type Page } from "@playwright/test"

export const desktopRoot = path.resolve(__dirname, "..")
const testEnvironment = { ...process.env, AGORA_DESKTOP_TEST_MODE: "1" }

/** Starts the unpackaged host on its own profile; `env` adds variables for this launch only. */
export async function launch(userData: string, env: Record<string, string> = {}): Promise<{ app: ElectronApplication; page: Page }> {
  const app = await electron.launch({ args: [desktopRoot, `--agora-test-user-data-dir=${userData}`], env: { ...testEnvironment, ...env }, timeout: 120_000 })
  return { app, page: await app.firstWindow() }
}

export async function quit(app: ElectronApplication): Promise<void> {
  const child = app.process()
  if (child.exitCode !== null) return
  const exited = new Promise<void>(resolve => child.once("exit", () => resolve()))
  await app.evaluate(({ app: electronApp }) => { setTimeout(() => electronApp.exit(0), 0) }).catch(() => undefined)
  await Promise.race([exited, new Promise<void>(resolve => setTimeout(resolve, 10_000))])
  if (child.exitCode === null) child.kill()
}

export function runSecondInstance(userData: string): Promise<number | null> {
  const require = createRequire(path.join(desktopRoot, "package.json"))
  const electronExecutable = require("electron") as string
  return new Promise((resolve, reject) => {
    const child = spawn(electronExecutable, [desktopRoot, `--agora-test-user-data-dir=${userData}`], { windowsHide: true, stdio: "ignore", env: testEnvironment })
    child.once("error", reject)
    child.once("exit", resolve)
  })
}

export const bridgeShape = (page: Page) => page.evaluate(() => {
  const host = globalThis as unknown as { agoraDesktop?: Record<string, object>; agoraConnection?: object; process?: unknown; require?: unknown }
  return {
    desktop: host.agoraDesktop ? Object.keys(host.agoraDesktop).sort() : null,
    lifecycle: host.agoraDesktop ? Object.keys(host.agoraDesktop.lifecycle).sort() : null,
    connection: host.agoraConnection ? Object.keys(host.agoraConnection).sort() : null,
    nodeProcess: typeof host.process,
    nodeRequire: typeof host.require
  }
})
