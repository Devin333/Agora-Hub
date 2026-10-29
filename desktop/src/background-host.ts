import type { MenuItemConstructorOptions } from "electron"
import type { ConnectionMode, ConnectionPhase } from "./contracts"

/**
 * Background residency for the desktop host.
 *
 * Closing the window keeps the host and the loaded workspace alive behind a
 * notification-area icon, so reopening is instant; only an explicit quit (tray
 * menu, OS session end) runs the exit path. A launch carrying
 * {@link BACKGROUND_LAUNCH_ARGUMENT} (the login item) connects to the server
 * without showing the window.
 */
export const BACKGROUND_LAUNCH_ARGUMENT = "--agora-background"

export function isBackgroundLaunch(argv: readonly string[]): boolean {
  return argv.includes(BACKGROUND_LAUNCH_ARGUMENT)
}

export interface LoginItemApplication {
  readonly isPackaged: boolean
  getLoginItemSettings(options: { args: string[] }): { openAtLogin: boolean }
  setLoginItemSettings(settings: { openAtLogin: boolean; args: string[] }): void
}

/**
 * The per-user "start in the background at sign-in" switch. It is only offered
 * for the installed app: an unpackaged dev host would register the bare
 * Electron binary, which cannot start this app on its own.
 */
export class LaunchAtLogin {
  private readonly args = [BACKGROUND_LAUNCH_ARGUMENT]

  constructor(private readonly application: LoginItemApplication) {}

  get available(): boolean {
    return this.application.isPackaged
  }

  get enabled(): boolean {
    // Windows matches the Run entry by executable and arguments, so reads pass the same args as writes.
    return this.available && this.application.getLoginItemSettings({ args: this.args }).openAtLogin
  }

  set(enabled: boolean): void {
    if (!this.available) return
    this.application.setLoginItemSettings({ openAtLogin: enabled, args: this.args })
  }
}

export interface TrayLike {
  setToolTip(toolTip: string): void
  setContextMenu(menu: unknown): void
  on(event: "click" | "double-click", listener: () => void): unknown
  destroy(): void
  isDestroyed(): boolean
}

export interface BackgroundTrayActions {
  showWindow(): void
  changeConnection(): void
  stopLocalStack(): void
  showLocalStackLogs(): void
  quit(): void
}

export interface BackgroundTrayOptions {
  createTray(): TrayLike
  buildMenu(template: MenuItemConstructorOptions[]): unknown
  launchAtLogin: LaunchAtLogin
  actions: BackgroundTrayActions
  onLaunchAtLoginError?(error: unknown): void
  /** Names the system in the launch-at-login item; defaults to the running one. */
  platform?: NodeJS.Platform
}

export function launchAtLoginLabel(platform: NodeJS.Platform): string {
  if (platform === "win32") return "登录 Windows 时在后台启动"
  if (platform === "darwin") return "登录 Mac 时在后台启动"
  return "登录系统时在后台启动"
}

export const TRAY_TITLE = "Agora Paper Agent"

export class BackgroundTray {
  private readonly tray: TrayLike
  private status = "正在连接服务器…"
  private quitting = false
  private localMode = false

  constructor(private readonly options: BackgroundTrayOptions) {
    this.tray = options.createTray()
    // Windows opens the context menu on right click; a left click brings the window back.
    this.tray.on("click", () => this.showWindow())
    this.tray.on("double-click", () => this.showWindow())
    this.render()
  }

  /** Short connection state shown in the icon tooltip, e.g. "已连接 agora.example.com". */
  setStatus(status: string): void {
    if (status === this.status) return
    this.status = status
    this.render()
  }

  /** Local mode adds the items that control the stack on this computer. */
  setLocalMode(localMode: boolean): void {
    if (localMode === this.localMode) return
    this.localMode = localMode
    this.render()
  }

  /** While an approved quit is leaving the menu cannot start a second one. */
  setQuitting(quitting: boolean): void {
    if (quitting === this.quitting) return
    this.quitting = quitting
    this.render()
  }

  destroy(): void {
    if (!this.tray.isDestroyed()) this.tray.destroy()
  }

  menuTemplate(): MenuItemConstructorOptions[] {
    const { launchAtLogin } = this.options
    return [
      { label: "显示 Agora Paper Agent", enabled: !this.quitting, click: () => this.showWindow() },
      { label: "切换连接方式…", enabled: !this.quitting, click: () => this.options.actions.changeConnection() },
      ...(this.localMode
        ? [
          { type: "separator" },
          { label: "停止本机服务", enabled: !this.quitting, click: () => this.options.actions.stopLocalStack() },
          { label: "查看本机服务日志", enabled: !this.quitting, click: () => this.options.actions.showLocalStackLogs() }
        ] satisfies MenuItemConstructorOptions[]
        : []),
      { type: "separator" },
      {
        label: launchAtLoginLabel(this.options.platform ?? process.platform),
        type: "checkbox",
        checked: launchAtLogin.enabled,
        enabled: launchAtLogin.available && !this.quitting,
        click: item => this.toggleLaunchAtLogin(Boolean(item.checked))
      },
      { type: "separator" },
      { label: this.quitting ? "正在退出…" : "退出", enabled: !this.quitting, click: () => this.options.actions.quit() }
    ]
  }

  private showWindow(): void {
    if (!this.quitting) this.options.actions.showWindow()
  }

  private toggleLaunchAtLogin(enabled: boolean): void {
    try {
      this.options.launchAtLogin.set(enabled)
    } catch (error) {
      this.options.onLaunchAtLoginError?.(error)
    }
    // Re-read the OS state so the checkbox never claims a setting that did not apply.
    this.render()
  }

  private render(): void {
    if (this.tray.isDestroyed()) return
    this.tray.setToolTip(`${TRAY_TITLE} — ${this.quitting ? "正在退出…" : this.status}`)
    this.tray.setContextMenu(this.options.buildMenu(this.menuTemplate()))
  }
}

/** Tooltip wording for a connection phase; anything not connected never reads as healthy. */
export function trayStatusFor(phase: ConnectionPhase, origin: string | null, mode: ConnectionMode | null = null): string {
  const local = mode === "local"
  const host = origin ? new URL(origin).host : null
  switch (phase) {
    case "connected": return local ? "已连接本机服务" : host ? `已连接 ${host}` : "已连接服务器"
    case "connecting": return local ? "正在启动本机服务…" : "正在连接服务器…"
    case "failed": return local ? "本机服务不可用" : "无法连接服务器"
    case "editing": return "正在切换连接方式"
    case "unconfigured": return "尚未选择连接方式"
    case "local-setup": return "等待设置本机管理员"
  }
}
