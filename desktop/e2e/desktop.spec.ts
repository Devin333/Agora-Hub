import { mkdtemp, rm } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { createRequire } from "node:module"
import { spawn } from "node:child_process"
import { expect, test, _electron as electron } from "@playwright/test"

const desktopRoot = path.resolve(__dirname, "..")

test.describe("Agora Paper Agent Electron host", () => {
  test("opens a real isolated window with bounded geometry, isolated userData, exact narrow bridge, rejected startup-only capabilities, and one instance", async () => {
    const userData = await mkdtemp(path.join(os.tmpdir(), "agora-electron-e2e-"))
    const userDataArg = `--agora-test-user-data-dir=${userData}`
    const environment = { ...process.env, AGORA_DESKTOP_TEST_MODE: "1" }
    const electronApp = await electron.launch({ args: [desktopRoot, userDataArg], env: environment, timeout: 120_000 })
    try {
      const page = await electronApp.firstWindow()
      await expect.poll(() => page.url(), { timeout: 120_000 }).toMatch(/^file:.*\/startup\.html$/i)
      await expect(page.locator("#status")).not.toHaveText("", { timeout: 120_000 })

      const isolation = await electronApp.evaluate(({ app }) => {
        const testState = globalThis as typeof globalThis & { __AGORA_DESKTOP_TEST_LAYOUT__?: { userDataPath: string; dataRoot: string } }
        return { electronUserData: app.getPath("userData"), runtimeLayout: testState.__AGORA_DESKTOP_TEST_LAYOUT__ ?? null }
      })
      expect(path.resolve(isolation.electronUserData)).toBe(path.resolve(userData))
      expect(path.resolve(isolation.runtimeLayout?.userDataPath ?? "")).toBe(path.resolve(userData))
      expect(path.resolve(isolation.runtimeLayout?.dataRoot ?? "")).toBe(path.join(path.resolve(userData), "runtime"))

      const sizing = await electronApp.evaluate(({ BrowserWindow, screen }) => {
        const window = BrowserWindow.getAllWindows()[0]
        const bounds = window.getBounds()
        const minimum = window.getMinimumSize()
        const workArea = screen.getDisplayMatching(bounds).workArea
        return { bounds, minimum, workArea }
      })
      expect(sizing.minimum[0]).toBeLessThanOrEqual(sizing.workArea.width)
      expect(sizing.minimum[1]).toBeLessThanOrEqual(sizing.workArea.height)
      expect(sizing.bounds.width).toBeLessThanOrEqual(sizing.workArea.width)
      expect(sizing.bounds.height).toBeLessThanOrEqual(sizing.workArea.height)
      expect(sizing.bounds.x).toBeGreaterThanOrEqual(sizing.workArea.x)
      expect(sizing.bounds.y).toBeGreaterThanOrEqual(sizing.workArea.y)
      expect(sizing.bounds.x + sizing.bounds.width).toBeLessThanOrEqual(sizing.workArea.x + sizing.workArea.width)
      expect(sizing.bounds.y + sizing.bounds.height).toBeLessThanOrEqual(sizing.workArea.y + sizing.workArea.height)

      const bridge = await page.evaluate(() => {
        const host = globalThis as unknown as {
          agoraDesktop?: { runtime?: object; config?: object; credentials?: object; lifecycle?: object; export?: object; appearance?: object }
          process?: unknown
          require?: unknown
        }
        return {
          keys: Object.keys(host.agoraDesktop ?? {}).sort(),
          runtime: Object.keys(host.agoraDesktop?.runtime ?? {}).sort(),
          restore: Object.keys((host.agoraDesktop?.runtime as { restore?: object } | undefined)?.restore ?? {}).sort(),
          clearData: Object.keys((host.agoraDesktop?.runtime as { clearData?: object } | undefined)?.clearData ?? {}).sort(),
          config: Object.keys(host.agoraDesktop?.config ?? {}).sort(),
          credentials: Object.keys(host.agoraDesktop?.credentials ?? {}).sort(),
          lifecycle: Object.keys(host.agoraDesktop?.lifecycle ?? {}).sort(),
          export: Object.keys(host.agoraDesktop?.export ?? {}).sort(),
          appearance: Object.keys(host.agoraDesktop?.appearance ?? {}).sort(),
          nodeProcess: typeof host.process,
          nodeRequire: typeof host.require
        }
      })
      expect(bridge).toEqual({
        keys: ["appearance", "config", "credentials", "export", "lifecycle", "runtime", "version"],
        runtime: ["backup", "clearData", "diagnostics", "restore", "retry", "status"],
        restore: ["activate", "preview"],
        clearData: ["apply", "preview", "status"],
        config: ["apply", "preview", "show"],
        credentials: ["remove", "replace", "status"],
        lifecycle: ["onQuitRequested", "respondToQuit", "updateState"],
        export: ["save"],
        appearance: ["set"],
        nodeProcess: "undefined",
        nodeRequire: "undefined"
      })

      const rejectedStartupCalls = await page.evaluate(async () => {
        const api = (globalThis as unknown as {
          agoraDesktop: {
            config: { show(): Promise<unknown>; preview(value: unknown): Promise<unknown>; apply(value: unknown): Promise<unknown> }
            credentials: { status(): Promise<unknown>; replace(): Promise<unknown>; remove(): Promise<unknown> }
            appearance: { set(value: unknown): Promise<unknown> }
          }
        }).agoraDesktop
        const invoke = async (call: () => Promise<unknown>) => {
          try { await call(); return "resolved" }
          catch (error) { return error instanceof Error ? error.message : String(error) }
        }
        return {
          configShow: await invoke(() => api.config.show()),
          configPreview: await invoke(() => api.config.preview({ research: { llm: null, source: { apiUrl: "file:///C:/Windows/win.ini", timeoutSeconds: 30 } } })),
          configApply: await invoke(() => api.config.apply({ previewId: "not-owned" })),
          credentialStatus: await invoke(() => api.credentials.status()),
          credentialReplace: await invoke(() => api.credentials.replace()),
          credentialRemove: await invoke(() => api.credentials.remove()),
          appearanceSet: await invoke(() => api.appearance.set("light"))
        }
      })
      expect(rejectedStartupCalls).toEqual({
        configShow: "Error invoking remote method 'agora:config:show': Error: ipc_source_rejected",
        configPreview: "Error invoking remote method 'agora:config:preview': Error: ipc_source_rejected",
        configApply: "Error invoking remote method 'agora:config:apply': Error: ipc_source_rejected",
        credentialStatus: "Error invoking remote method 'agora:credential:status': Error: ipc_source_rejected",
        credentialReplace: "Error invoking remote method 'agora:credential:replace': Error: ipc_source_rejected",
        credentialRemove: "Error invoking remote method 'agora:credential:remove': Error: ipc_source_rejected",
        appearanceSet: "Error invoking remote method 'agora:appearance:set': Error: ipc_source_rejected"
      })

      // A fresh profile follows the OS; the frame and startup page paint the matching Rakazo background.
      const native = await electronApp.evaluate(({ BrowserWindow, nativeTheme }) => ({
        themeSource: nativeTheme.themeSource,
        dark: nativeTheme.shouldUseDarkColors,
        background: BrowserWindow.getAllWindows()[0].getBackgroundColor().toLowerCase()
      }))
      expect(native.themeSource).toBe("system")
      expect(native.background).toBe(native.dark ? "#161616" : "#ffffff")
      expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe(native.dark ? "rgb(22, 22, 22)" : "rgb(255, 255, 255)")

      const pagesBefore = electronApp.windows().length
      const opened = await page.evaluate(() => (globalThis as unknown as { open(url: string): unknown }).open("file:///C:/Windows/win.ini"))
      expect(opened).toBeNull()
      await expect.poll(() => electronApp.windows().length).toBe(pagesBefore)

      const require = createRequire(path.join(desktopRoot, "package.json"))
      const electronExecutable = require("electron") as string
      const duplicateExit = await new Promise<number | null>((resolve, reject) => {
        const child = spawn(electronExecutable, [desktopRoot, userDataArg], { windowsHide: true, stdio: "ignore", env: environment })
        child.once("error", reject)
        child.once("exit", resolve)
      })
      expect(duplicateExit).toBe(0)
      await expect.poll(() => electronApp.windows().length).toBe(1)
    } finally {
      const process = electronApp.process()
      if (process.exitCode === null) {
        const exited = new Promise<void>(resolve => process.once("exit", () => resolve()))
        await electronApp.evaluate(({ app }) => {
          setTimeout(() => app.exit(0), 0)
        }).catch(() => undefined)
        await Promise.race([exited, new Promise<void>(resolve => setTimeout(resolve, 10_000))])
        if (process.exitCode === null) process.kill()
      }
      await rm(userData, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
    }
  })
})

