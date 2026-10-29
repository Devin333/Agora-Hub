import path from "node:path"
import { app, BrowserWindow, dialog, Menu, nativeImage, nativeTheme, net, screen, session, shell, Tray, type MessageBoxOptions } from "electron"
import { appearanceFilePath, DesktopAppearance, windowBackgroundFor } from "./appearance"
import { AuditLogger } from "./audit-log"
import { BackgroundTray, isBackgroundLaunch, LaunchAtLogin, trayStatusFor } from "./background-host"
import { HOST_EVENTS } from "./channels"
import type { ConnectionStatusView, QuitReason } from "./contracts"
import { registerIpcHandlers } from "./ipc"
import { RendererLifecycleBridge, requiresNativeQuitConfirmation, type RendererQuitAssessment } from "./lifecycle-bridge"
import { decideNavigation, isConnectionPage, isWorkspaceUrl } from "./navigation-policy"
import { redactText } from "./redaction"
import { bundledServerFilePath, connectionFilePath, readBundledServerOrigin, ServerConnectionStore } from "./server-connection"
import { probeServer } from "./server-probe"
import { acquireSingleInstanceLock } from "./single-instance"
import { resolveTestUserDataOverride } from "./user-data-override"
import { focusExistingWindow } from "./window-focus"
import { secureWindowOptions } from "./window-options"
import { WorkspaceConnection } from "./workspace-connection"

const testUserDataPath = resolveTestUserDataOverride({
  argv: process.argv,
  isPackaged: app.isPackaged,
  testMode: process.env.AGORA_DESKTOP_TEST_MODE === "1"
})
if (testUserDataPath) app.setPath("userData", testUserDataPath)

const launchedInBackground = isBackgroundLaunch(process.argv)
const gotSingleInstanceLock = acquireSingleInstanceLock(app)
const CONNECTION_PAGE = path.join(__dirname, "connect.html")

let mainWindow: BrowserWindow | null = null
let connection: WorkspaceConnection | null = null
let logger: AuditLogger | null = null
let appearance: DesktopAppearance | null = null
let backgroundTray: BackgroundTray | null = null
let removeIpcHandlers: (() => void) | null = null
let allowWindowClose = false
let allowAppQuit = false
let leaveInProgress = false
const lifecycle = new RendererLifecycleBridge()

function log(event: string, details: Record<string, unknown> = {}): void {
  void logger?.write(event, details)
}

function errorText(error: unknown): string {
  return redactText(error instanceof Error ? error.message : error, 300)
}

async function openExternalUrl(url: string): Promise<void> {
  try {
    await shell.openExternal(url, { activate: true })
    log("external_url_opened", { protocol: new URL(url).protocol, host: new URL(url).host })
  } catch (error) {
    log("external_url_failed", { error: errorText(error) })
  }
}

function workspaceOrigin(): string | null {
  return connection?.workspaceOrigin ?? null
}

function installWebContentsPolicy(window: BrowserWindow): void {
  const contents = window.webContents
  const handleNavigation = (event: Electron.Event, target: string, redirect: boolean) => {
    const decision = decideNavigation(target, workspaceOrigin(), CONNECTION_PAGE)
    if (decision.action === "allow") return
    event.preventDefault()
    if (redirect) {
      // A server answer that leaves the workspace is refused, never followed elsewhere.
      log("workspace_redirect_refused", { protocol: safeProtocol(target) })
      connection?.redirectRefused()
    } else if (decision.action === "external" && decision.url) {
      void openExternalUrl(decision.url)
    }
  }
  contents.on("will-navigate", (event, target) => handleNavigation(event, target, false))
  contents.on("will-redirect", (event, target) => handleNavigation(event, target, true))
  contents.on("will-attach-webview", event => event.preventDefault())
  contents.setWindowOpenHandler(details => {
    const decision = decideNavigation(details.url, workspaceOrigin(), CONNECTION_PAGE)
    if (decision.action === "external" && decision.url) void openExternalUrl(decision.url)
    return { action: "deny" }
  })
  contents.session.on("will-download", (_event, item) => {
    item.cancel()
    log("renderer_download_blocked", { fileName: item.getFilename() })
  })
  contents.on("did-navigate", (_event, url, httpResponseCode) => {
    // A new document starts its lifecycle revisions over; the old one's report no longer holds.
    lifecycle.documentReplaced()
    connection?.mainFrameNavigated(url, httpResponseCode)
  })
  contents.on("did-fail-load", (_event, _code, description, _url, isMainFrame) => {
    if (isMainFrame) connection?.mainFrameFailed(description)
  })
  contents.on("render-process-gone", (_event, details) => {
    lifecycle.rendererGone()
    log("renderer_process_gone", { reason: details.reason, exitCode: details.exitCode })
    connection?.rendererGone()
  })
  contents.on("did-finish-load", () => {
    if (connection && isConnectionPage(contents.getURL(), CONNECTION_PAGE)) contents.send(HOST_EVENTS.connectionChanged, connection.status)
  })
}

function safeProtocol(raw: string): string {
  try {
    return new URL(raw).protocol
  } catch {
    return "invalid"
  }
}

function createWindow(revealWhenReady: boolean): BrowserWindow {
  const selectedDisplay = screen.getDisplayNearestPoint(screen.getCursorScreenPoint())
  const window = new BrowserWindow(secureWindowOptions(__dirname, selectedDisplay.workArea, appearance?.view.theme ?? "dark"))
  installWebContentsPolicy(window)
  if (revealWhenReady) window.once("ready-to-show", () => window.show())
  window.on("close", event => {
    if (allowWindowClose) return
    event.preventDefault()
    // With a tray icon, closing only hides the window: the workspace stays loaded
    // and nothing unsaved is at risk, so no exit assessment is due.
    if (backgroundTray) {
      if (!leaveInProgress) window.hide()
      return
    }
    void requestClose("window-close")
  })
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null
  })
  return window
}

function showMainWindow(): void {
  if (mainWindow && !mainWindow.isDestroyed()) focusExistingWindow(mainWindow)
}

function showsWorkspace(window: BrowserWindow): boolean {
  const origin = workspaceOrigin()
  return origin !== null && isWorkspaceUrl(window.webContents.getURL(), origin)
}

const LEAVE_TITLES: Record<QuitReason, { title: string; message: string; proceed: string }> = {
  "window-close": { title: "确认关闭 Agora Paper Agent", message: "要关闭论文工作台吗？", proceed: "仍然关闭" },
  "app-quit": { title: "确认退出 Agora Paper Agent", message: "应用正在请求退出", proceed: "仍然退出" },
  "server-change": { title: "确认更换服务器地址", message: "更换地址会离开当前工作台", proceed: "仍然更换" }
}

async function confirmLeaveWithNativeDialog(reason: QuitReason, assessment: RendererQuitAssessment): Promise<boolean> {
  const state = assessment.status === "confirmed" ? assessment.state : assessment.lastKnownState
  const wording = LEAVE_TITLES[reason]
  const detail = [
    assessment.status === "unknown" ? "无法从当前页面确认是否还有未保存的内容。" : null,
    state.dirty ? "页面最近报告有尚未保存的编辑。" : null,
    state.activeTaskCount > 0 ? `页面最近报告仍有 ${state.activeTaskCount} 个进行中的请求。` : null,
    "已保存到服务器的内容不受影响；离开后，未保存的草稿会丢失，进行中的请求会被中断。"
  ].filter(Boolean).join("\n")
  const options: MessageBoxOptions = {
    type: "warning",
    title: wording.title,
    message: assessment.status === "unknown" ? "页面保存状态未知" : wording.message,
    detail,
    buttons: ["留在工作台", wording.proceed],
    defaultId: 0,
    cancelId: 0,
    noLink: true
  }
  const result = mainWindow && !mainWindow.isDestroyed()
    ? await dialog.showMessageBox(mainWindow, options)
    : await dialog.showMessageBox(options)
  return result.response === 1
}

/** Asks the workspace (and, when it cannot vouch for itself, the user) whether leaving it now is fine. */
async function confirmLeavingWorkspace(reason: QuitReason): Promise<boolean> {
  if (!mainWindow || mainWindow.isDestroyed() || !showsWorkspace(mainWindow)) return true
  const assessment = await lifecycle.requestQuit(mainWindow, reason)
  if (assessment.status === "confirmed" && assessment.decision === "cancel") return false
  if (assessment.status === "unknown") log("renderer_quit_state_unknown", { reason: assessment.reason, revision: assessment.lastKnownState.revision })
  if (!requiresNativeQuitConfirmation(assessment)) return true
  showMainWindow()
  return await confirmLeaveWithNativeDialog(reason, assessment)
}

async function requestClose(reason: "window-close" | "app-quit"): Promise<void> {
  if (leaveInProgress) return
  leaveInProgress = true
  try {
    if (!(await confirmLeavingWorkspace(reason))) return
    backgroundTray?.setQuitting(true)
    allowWindowClose = true
    allowAppQuit = true
    lifecycle.cancelPending()
    log("host_exit", { reason })
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.close()
    app.quit()
  } finally {
    leaveInProgress = false
  }
}

async function changeServer(): Promise<void> {
  if (leaveInProgress || !connection) return
  leaveInProgress = true
  try {
    showMainWindow()
    if (!(await confirmLeavingWorkspace("server-change"))) return
    await connection.edit()
  } finally {
    leaveInProgress = false
  }
}

/** The 256px app icon pre-scaled per DPI step so the notification area never shows a blurry downscale. */
function trayIcon(): Electron.NativeImage {
  const source = nativeImage.createFromPath(path.join(__dirname, "icon.png"))
  const icon = nativeImage.createEmpty()
  for (const scaleFactor of [1, 1.25, 1.5, 2]) {
    const size = Math.round(16 * scaleFactor)
    icon.addRepresentation({ scaleFactor, width: size, height: size, buffer: source.resize({ width: size, height: size, quality: "best" }).toPNG() })
  }
  return icon
}

function createBackgroundTray(): void {
  try {
    backgroundTray = new BackgroundTray({
      createTray: () => new Tray(trayIcon()),
      buildMenu: template => Menu.buildFromTemplate(template),
      launchAtLogin: new LaunchAtLogin(app),
      actions: { showWindow: showMainWindow, changeServer: () => void changeServer(), quit: () => void requestClose("app-quit") },
      onLaunchAtLoginError: error => log("launch_at_login_update_failed", { error: errorText(error) })
    })
  } catch (error) {
    // Without a notification-area icon a hidden window could not be reopened,
    // so closing falls back to the full exit path.
    backgroundTray = null
    log("background_tray_unavailable", { error: errorText(error) })
  }
}

function reflectConnection(status: ConnectionStatusView): void {
  backgroundTray?.setStatus(trayStatusFor(status.phase, status.origin))
  const window = mainWindow
  if (!window || window.isDestroyed()) return
  // Without an address there is nothing to run in the background, so the window asks for one.
  // A lost connection only shows in the tray: the user hid the window and decides when to look.
  if (status.phase === "unconfigured") showMainWindow()
  if (isConnectionPage(window.webContents.getURL(), CONNECTION_PAGE)) window.webContents.send(HOST_EVENTS.connectionChanged, status)
}

function createConnection(window: BrowserWindow): WorkspaceConnection {
  const policy = { allowLoopbackHttp: !app.isPackaged }
  const store = new ServerConnectionStore({
    connectionFile: connectionFilePath(app.getPath("userData")),
    bundledOrigin: readBundledServerOrigin(bundledServerFilePath(__dirname), policy),
    policy
  })
  return new WorkspaceConnection({
    store,
    probe: origin => probeServer((url, init) => net.fetch(url, init), origin),
    surface: {
      showConnectionPage: async () => {
        if (window.isDestroyed()) return
        // A workspace load still in flight must not land after the attempt that started it was abandoned.
        if (window.webContents.isLoading()) window.webContents.stop()
        if (isConnectionPage(window.webContents.getURL(), CONNECTION_PAGE)) return
        await window.loadFile(CONNECTION_PAGE)
      },
      showWorkspace: async url => {
        if (!window.isDestroyed()) await window.loadURL(url)
      },
      currentUrl: () => window.isDestroyed() ? "" : window.webContents.getURL()
    },
    timer: { set: (callback, delay) => setTimeout(callback, delay), clear: handle => clearTimeout(handle as NodeJS.Timeout), now: () => Date.now() },
    isWorkspaceUrl,
    onEvent: (event, details) => log(event, details)
  })
}

async function bootstrap(): Promise<void> {
  app.setName("Agora Paper Agent")
  app.setAppUserModelId("com.agorahub.paperagent")
  Menu.setApplicationMenu(null)
  logger = new AuditLogger(path.join(app.getPath("userData"), "logs"))
  log("host_start", { version: app.getVersion(), packaged: app.isPackaged })
  appearance = new DesktopAppearance(nativeTheme, appearanceFilePath(app.getPath("userData")))
  appearance.onChange(view => {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.setBackgroundColor(windowBackgroundFor(view.theme))
  })
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false))
  session.defaultSession.setPermissionCheckHandler(() => false)

  createBackgroundTray()
  // A login-item launch stays hidden behind the tray icon until the user opens it.
  const window = createWindow(!(launchedInBackground && backgroundTray))
  mainWindow = window
  connection = createConnection(window)
  connection.onChange(reflectConnection)
  removeIpcHandlers = registerIpcHandlers({
    connection,
    lifecycle,
    appearance,
    connectionFile: CONNECTION_PAGE,
    documentsPath: app.getPath("documents"),
    logger: { write: async (event, details) => log(event, details) }
  })
  await connection.connect()
}

if (!gotSingleInstanceLock) {
  app.quit()
} else {
  app.on("second-instance", (_event, argv) => {
    // A login-item launch while the host already runs has nothing to reveal.
    if (isBackgroundLaunch(argv)) return
    log("second_instance_focused")
    showMainWindow()
  })
  app.on("before-quit", event => {
    if (allowAppQuit) return
    event.preventDefault()
    void requestClose("app-quit")
  })
  app.on("window-all-closed", () => {
    if (allowAppQuit) app.quit()
  })
  app.on("will-quit", () => {
    connection?.dispose()
    connection = null
    backgroundTray?.destroy()
    backgroundTray = null
    removeIpcHandlers?.()
    removeIpcHandlers = null
    appearance?.dispose()
    appearance = null
  })
  process.on("uncaughtException", error => {
    log("host_uncaught_exception", { name: error.name, message: error.message })
  })
  process.on("unhandledRejection", reason => {
    log("host_unhandled_rejection", { message: reason instanceof Error ? reason.message : String(reason) })
  })
  void app.whenReady().then(bootstrap)
}
