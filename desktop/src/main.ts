import path from "node:path"
import { pathToFileURL } from "node:url"
import { app, BrowserWindow, dialog, Menu, nativeTheme, screen, session, shell, type MessageBoxOptions } from "electron"
import { AuditLogger } from "./audit-log"
import { registerIpcHandlers } from "./ipc"
import { RendererLifecycleBridge, requiresNativeQuitConfirmation, type RendererQuitAssessment } from "./lifecycle-bridge"
import { decideNavigation, isAllowedApplicationUrl, isTrustedIpcSource, paperAgentUrl } from "./navigation-policy"
import { RuntimeClient } from "./runtime-client"
import { RuntimeCoordinator, UnavailableRuntimeController, type RuntimeController } from "./runtime-coordinator"
import { RuntimeMaintenanceService, UnavailableRuntimeMaintenanceController, type RuntimeMaintenanceController } from "./runtime-maintenance"
import { RestoreArchiveSnapshotStore } from "./restore-archive-snapshot"
import { DesktopConfigBridge, UnavailableDesktopConfigBridge, type DesktopConfigController } from "./desktop-config-bridge"
import { NativeCredentialBridge, UnavailableCredentialBridge, type CredentialController } from "./native-credential-bridge"
import { resolveRuntimeLayout } from "./runtime-layout"
import type { RuntimeStatusView } from "./contracts"
import { redactText } from "./redaction"
import { focusExistingWindow } from "./window-focus"
import { secureWindowOptions } from "./window-options"
import { appearanceFilePath, DesktopAppearance, windowBackgroundFor } from "./appearance"
import { acquireSingleInstanceLock } from "./single-instance"
import { resolveTestUserDataOverride } from "./user-data-override"
import { OwnedDataClearService, UnavailableOwnedDataClearController, type OwnedDataClearController, type OwnedDataClearFinalizerRequest } from "./owned-data-clear"
import { OwnedWindowMaintenanceFence } from "./owned-window-maintenance"
import { readWindowsElectronProcessIdentity } from "./electron-process-identity"
import { armOwnedDataClearFinalizer } from "./owned-data-clear-exit"

const testUserDataPath = resolveTestUserDataOverride({
  argv: process.argv,
  isPackaged: app.isPackaged,
  testMode: process.env.AGORA_DESKTOP_TEST_MODE === "1"
})
if (testUserDataPath) app.setPath("userData", testUserDataPath)

const HOST_VERSION = app.getVersion()
const gotSingleInstanceLock = acquireSingleInstanceLock(app)

let mainWindow: BrowserWindow | null = null
let coordinator: RuntimeController | null = null
let maintenance: RuntimeMaintenanceController = new UnavailableRuntimeMaintenanceController()
let configBridge: DesktopConfigController = new UnavailableDesktopConfigBridge()
let credentialBridge: CredentialController = new UnavailableCredentialBridge()
let clearData: OwnedDataClearController = new UnavailableOwnedDataClearController()
let logger: AuditLogger | null = null
let canonicalOrigin: string | null = null
let allowWindowClose = false
let allowAppQuit = false
let closeInProgress = false
let removeIpcHandlers: (() => void) | null = null
let dataClearExitArmed = false
let appearance: DesktopAppearance | null = null
const lifecycle = new RendererLifecycleBridge()
const maintenanceFence = new OwnedWindowMaintenanceFence()
const nullLogger = { write: async (_event: string, _details: Record<string, unknown> = {}) => undefined }

function startupFilePath(): string {
  return path.join(__dirname, "startup.html")
}

async function openExternalUrl(url: string): Promise<void> {
  try {
    await shell.openExternal(url, { activate: true })
    await logger?.write("external_url_opened", { protocol: new URL(url).protocol, host: new URL(url).host })
  } catch (error) {
    await logger?.write("external_url_failed", { error: error instanceof Error ? error.message : "unknown" })
  }
}

function installWebContentsPolicy(window: BrowserWindow): void {
  const startupUrl = pathToFileURL(startupFilePath()).href
  const handleNavigation = (event: Electron.Event, target: string) => {
    if (maintenanceFence.blocksNavigation(window.webContents.id)) {
      event.preventDefault()
      return
    }
    if (target === startupUrl) {
      maintenanceFence.revokeDocument(window.webContents.id)
      return
    }
    if (!canonicalOrigin) {
      event.preventDefault()
      return
    }
    const decision = decideNavigation(target, canonicalOrigin)
    if (decision.action === "allow") {
      maintenanceFence.revokeDocument(window.webContents.id)
      return
    }
    event.preventDefault()
    if (decision.action === "external" && decision.url) void openExternalUrl(decision.url)
  }

  window.webContents.on("will-navigate", handleNavigation)
  window.webContents.on("will-redirect", handleNavigation)
  window.webContents.on("will-attach-webview", event => event.preventDefault())
  window.webContents.setWindowOpenHandler(details => {
    if (canonicalOrigin) {
      const decision = decideNavigation(details.url, canonicalOrigin)
      if (decision.action === "external" && decision.url) void openExternalUrl(decision.url)
    }
    return { action: "deny" }
  })
  window.webContents.session.on("will-download", (_event, item) => {
    item.cancel()
    void logger?.write("renderer_download_blocked", { fileName: item.getFilename() })
  })
  window.webContents.on("before-input-event", (event, input) => {
    if (maintenanceFence.blocksInputReload(window.webContents.id, input.key, input.control, input.meta, input.shift)) event.preventDefault()
  })
  window.webContents.session.webRequest.onBeforeRequest((details, callback) => {
    callback(maintenanceFence.blocksRequest(details) ? { cancel: true } : {})
  })
  window.webContents.on("render-process-gone", (_event, details) => {
    lifecycle.rendererGone()
    maintenanceFence.revokeDocument(window.webContents.id)
    void logger?.write("renderer_process_gone", { reason: details.reason, exitCode: details.exitCode })
    if (!window.isDestroyed()) void window.loadFile(startupFilePath())
  })
}

function createWindow(): BrowserWindow {
  const selectedDisplay = screen.getDisplayNearestPoint(screen.getCursorScreenPoint())
  const window = new BrowserWindow(secureWindowOptions(__dirname, selectedDisplay.workArea, appearance?.view.theme ?? "dark"))
  installWebContentsPolicy(window)
  window.once("ready-to-show", () => window.show())
  window.on("close", event => {
    if (allowWindowClose) return
    event.preventDefault()
    void requestClose("window-close")
  })
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null
  })
  void window.loadFile(startupFilePath())
  return window
}

async function confirmCloseWithNativeDialog(reason: "window-close" | "app-quit", assessment: RendererQuitAssessment): Promise<boolean> {
  const state = assessment.status === "confirmed" ? assessment.state : assessment.lastKnownState
  const detail = [
    assessment.status === "unknown" ? "无法从当前页面取得与最新 revision 匹配的退出确认；未保存内容和活动任务状态未知。" : null,
    assessment.status === "unknown" ? `通信结果：${assessment.reason}。旧快照只供参考，不能证明草稿已保存。` : null,
    state.dirty ? "最近一次有效状态显示存在尚未确认已保存的编辑。" : null,
    state.activeTaskCount > 0 ? `最近一次有效状态显示仍有 ${state.activeTaskCount} 个活动任务。` : null,
    "继续退出只会请求既有 runtime manager 安全收敛；runtime drain/stop 成功不代表草稿已保存。",
    "默认保留窗口；宿主不会强杀身份不明的进程。"
  ].filter(Boolean).join("\n")
  const options: MessageBoxOptions = {
    type: "warning",
    title: "确认退出 Agora Paper Agent",
    message: assessment.status === "unknown"
      ? "页面保存与运行状态未知"
      : reason === "app-quit" ? "应用正在请求退出" : "要关闭论文工作台吗？",
    detail,
    buttons: ["保留窗口", "仍然退出并安全收敛"],
    defaultId: 0,
    cancelId: 0,
    noLink: true
  }
  const result = mainWindow && !mainWindow.isDestroyed()
    ? await dialog.showMessageBox(mainWindow, options)
    : await dialog.showMessageBox(options)
  return result.response === 1
}

function setHostExitTitle(status: string): void {
  if (!mainWindow || mainWindow.isDestroyed()) return
  mainWindow.setTitle(`Agora Paper Agent — ${status}`)
}

async function stopOwnedRuntime(): Promise<boolean> {
  if (!coordinator) return true
  const before = coordinator.status
  setHostExitTitle("正在安全收敛本机服务…")
  await logger?.write("runtime_stop_requested", { phase: before.phase, code: before.code })
  const stopped = await coordinator.stop()
  if (stopped.phase === "stopped") {
    setHostExitTitle("本机服务已停止，正在退出…")
    await logger?.write("runtime_stopped", { state: stopped.state, code: stopped.code })
    return true
  }
  setHostExitTitle("退出未完成（本机服务状态未知）")
  await logger?.write("runtime_stop_unconfirmed", { phase: stopped.phase, state: stopped.state, code: stopped.code })
  if (!mainWindow || mainWindow.isDestroyed()) return false
  await dialog.showMessageBox(mainWindow, {
    type: "warning",
    title: "本机服务状态未确认",
    message: "runtime manager 未确认完成 drain 与进程收敛，宿主将保持打开。",
    detail: `${stopped.message}\n不会终止身份不明的进程，也不会把无 JSON、超时或取消确认当作已停止。请保留窗口；待当前启动命令或 owner 状态收敛后，可再次关闭以重新执行 stop 核验，或查看只读诊断。`,
    buttons: ["保留窗口"],
    defaultId: 0,
    cancelId: 0,
    noLink: true
  })
  return false
}

async function requestClose(reason: "window-close" | "app-quit"): Promise<void> {
  if (closeInProgress) return
  closeInProgress = true
  const originalTitle = mainWindow && !mainWindow.isDestroyed() ? mainWindow.getTitle() : null
  let exitApproved = false
  let retainExitStatus = false
  try {
    if (clearData.destructiveExitPending && !dataClearExitArmed) {
      const options: MessageBoxOptions = {
        type: "warning", title: "数据清除仍在受控恢复中", message: "当前存在 PREPARED 或 finalizer 未就绪的清除事务。",
        detail: "为避免未确认的 finalizer 在普通退出后删除数据，宿主保持打开。请在当前页面重新预览并明确确认，或检查状态。",
        buttons: ["返回清除恢复"], defaultId: 0, cancelId: 0, noLink: true
      }
      if (mainWindow && !mainWindow.isDestroyed()) await dialog.showMessageBox(mainWindow, options)
      else await dialog.showMessageBox(options)
      return
    }
    setHostExitTitle("正在确认页面状态…")
    let rendererAssessment: RendererQuitAssessment = {
      status: "unknown",
      reason: "renderer-unavailable",
      lastKnownState: lifecycle.snapshot()
    }
    if (mainWindow && !mainWindow.isDestroyed()) rendererAssessment = await lifecycle.requestQuit(mainWindow, reason)
    if (rendererAssessment.status === "confirmed" && rendererAssessment.decision === "cancel") return
    if (rendererAssessment.status === "unknown") {
      await logger?.write("renderer_quit_state_unknown", { reason: rendererAssessment.reason, revision: rendererAssessment.lastKnownState.revision })
    }
    if (requiresNativeQuitConfirmation(rendererAssessment) && !(await confirmCloseWithNativeDialog(reason, rendererAssessment))) return
    if (!(await stopOwnedRuntime())) {
      retainExitStatus = true
      return
    }
    exitApproved = true
    allowWindowClose = true
    allowAppQuit = true
    lifecycle.cancelPending()
    await logger?.write("host_exit", { reason })
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.close()
    app.quit()
  } finally {
    if (!exitApproved && !retainExitStatus && originalTitle && mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.setTitle(originalTitle)
    }
    closeInProgress = false
  }
}

async function prepareOwnedDataClearExit(client: RuntimeClient, request: OwnedDataClearFinalizerRequest): Promise<boolean> {
  if (dataClearExitArmed) return false
  try {
    const ownedSession = mainWindow && !mainWindow.isDestroyed() ? mainWindow.webContents.session : session.defaultSession
    await armOwnedDataClearFinalizer(ownedSession, () => client.launchDataClearFinalizer(request))
    dataClearExitArmed = true
    await logger?.write("data_clear_finalizer_ready", { operationId: request.operationId })
    setTimeout(() => {
      allowWindowClose = true
      allowAppQuit = true
      lifecycle.cancelPending()
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.close()
      app.quit()
    }, 100)
    return true
  } catch (error) {
    await logger?.write("data_clear_host_exit_not_ready", {
      code: error && typeof error === "object" && "code" in error ? String(error.code) : "desktop_data_clear_host_exit_not_ready"
    })
    return false
  }
}
function navigateWhenReady(status: RuntimeStatusView): void {
  if (!status.localTransportReady || !status.canonicalOrigin || !mainWindow || mainWindow.isDestroyed()) return
  canonicalOrigin = status.canonicalOrigin
  const target = paperAgentUrl(status.canonicalOrigin)
  const current = mainWindow.webContents.getURL()
  if (current === target || isTrustedIpcSource(current, canonicalOrigin, startupFilePath()) && current.startsWith(canonicalOrigin)) return
  void logger?.write(status.phase === "ready" ? "runtime_ready" : "runtime_local_transport_ready", { state: status.state, code: status.code, instanceKey: status.instanceKey })
  const ownedWindow = mainWindow
  void ownedWindow.loadURL(target).then(() => {
    if (ownedWindow.isDestroyed()) return
    const loadedUrl = ownedWindow.webContents.getURL()
    const frame = ownedWindow.webContents.mainFrame
    if (!frame.isDestroyed() && isAllowedApplicationUrl(loadedUrl, status.canonicalOrigin as string)) {
      maintenanceFence.authorizeDocument(ownedWindow.webContents.id, frame.processId, frame.routingId)
    }
  }).catch(async error => {
    await logger?.write("frontend_load_failed", { error: redactText(error instanceof Error ? error.message : error, 500) })
    if (!ownedWindow.isDestroyed()) await ownedWindow.loadFile(startupFilePath())
  })
}

async function bootstrap(): Promise<void> {
  app.setName("Agora Paper Agent")
  app.setAppUserModelId("com.agorahub.paperagent")
  Menu.setApplicationMenu(null)
  appearance = new DesktopAppearance(nativeTheme, appearanceFilePath(app.getPath("userData")))
  appearance.onChange(view => {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.setBackgroundColor(windowBackgroundFor(view.theme))
  })
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false))
  session.defaultSession.setPermissionCheckHandler(() => false)

  let startupBlocked = true
  try {
    const layout = await resolveRuntimeLayout({
      isPackaged: app.isPackaged,
      appPath: app.getAppPath(),
      resourcesPath: process.resourcesPath,
      userDataPath: app.getPath("userData")
    })
    if (testUserDataPath) {
      const testState = globalThis as typeof globalThis & { __AGORA_DESKTOP_TEST_LAYOUT__?: { userDataPath: string; dataRoot: string } }
      testState.__AGORA_DESKTOP_TEST_LAYOUT__ = { userDataPath: app.getPath("userData"), dataRoot: layout.dataRoot }
    }
    const client = new RuntimeClient(layout)
    clearData = new OwnedDataClearService(
      client,
      () => readWindowsElectronProcessIdentity(process.pid),
      request => prepareOwnedDataClearExit(client, request)
    )
    const clearStatus = await clearData.status()
    startupBlocked = clearData.businessAdmissionClosed
    if (startupBlocked) {
      const error = Object.assign(new Error(clearStatus.message), { code: clearStatus.code })
      coordinator = new UnavailableRuntimeController(error, HOST_VERSION, app.isPackaged)
    } else {
      logger = new AuditLogger(path.join(app.getPath("userData"), "logs"))
      await logger.write("host_start", { version: HOST_VERSION, packaged: app.isPackaged, dataClearState: clearStatus.state })
      coordinator = new RuntimeCoordinator(client, HOST_VERSION, app.isPackaged)
      maintenance = new RuntimeMaintenanceService(client, coordinator, new RestoreArchiveSnapshotStore(path.join(layout.dataRoot, "desktop", "restore-snapshots")))
      configBridge = new DesktopConfigBridge(client, coordinator)
      credentialBridge = new NativeCredentialBridge(client)
    }
  } catch (error) {
    coordinator = new UnavailableRuntimeController(error, HOST_VERSION, app.isPackaged)
    maintenance = new UnavailableRuntimeMaintenanceController()
    configBridge = new UnavailableDesktopConfigBridge()
    credentialBridge = new UnavailableCredentialBridge()
    clearData = new UnavailableOwnedDataClearController()
    await logger?.write("runtime_configuration_failed", {
      code: error && typeof error === "object" && "code" in error ? String(error.code) : "runtime_configuration_failed",
      message: error instanceof Error ? error.message : "Unknown runtime configuration failure"
    })
  }

  removeIpcHandlers = registerIpcHandlers({
    coordinator,
    lifecycle,
    maintenance,
    config: configBridge,
    credentials: credentialBridge,
    clearData,
    maintenanceFence,
    appearance,
    startupFile: startupFilePath(),
    getCanonicalOrigin: () => canonicalOrigin,
    documentsPath: app.getPath("documents"),
    logger: logger ?? nullLogger,
    onRuntimeReady: navigateWhenReady
  })
  mainWindow = createWindow()
  const status = await coordinator.initialize()
  if (!startupBlocked) navigateWhenReady(status)
  if (startupBlocked) await logger?.write("data_clear_startup_blocked", { code: status.code, state: status.state })
  else if (status.localTransportReady && status.phase === "degraded") await logger?.write("runtime_remote_capabilities_limited", { phase: status.phase, state: status.state, code: status.code })
  else if (status.phase !== "ready") await logger?.write("runtime_start_incomplete", { phase: status.phase, state: status.state, code: status.code })
}
if (!gotSingleInstanceLock) {
  app.quit()
} else {
  app.on("second-instance", () => {
    void logger?.write("second_instance_focused")
    focusExistingWindow(mainWindow)
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
    removeIpcHandlers?.()
    removeIpcHandlers = null
    appearance?.dispose()
    appearance = null
  })
  process.on("uncaughtException", error => {
    void logger?.write("host_uncaught_exception", { name: error.name, message: error.message })
  })
  process.on("unhandledRejection", reason => {
    void logger?.write("host_unhandled_rejection", { message: reason instanceof Error ? reason.message : String(reason) })
  })
  void app.whenReady().then(bootstrap)
}


















