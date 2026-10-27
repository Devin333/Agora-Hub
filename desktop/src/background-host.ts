import type { MenuItemConstructorOptions } from "electron"

/**
 * Background residency for the desktop host.
 *
 * Closing the window keeps the host and its owned runtime alive behind a
 * notification-area icon, so reopening is instant; only an explicit quit (tray
 * menu, OS session end) runs the drain-and-stop exit path. A launch carrying
 * {@link BACKGROUND_LAUNCH_ARGUMENT} (the login item) starts the runtime without
 * showing the window.
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
  quit(): void
}

export interface BackgroundTrayOptions {
  createTray(): TrayLike
  buildMenu(template: MenuItemConstructorOptions[]): unknown
  launchAtLogin: LaunchAtLogin
  actions: BackgroundTrayActions
  onLaunchAtLoginError?(error: unknown): void
}

export const TRAY_TITLE = "Agora Paper Agent"

export class BackgroundTray {
  private readonly tray: TrayLike
  private status = "正在启动本机服务…"
  private quitting = false

  constructor(private readonly options: BackgroundTrayOptions) {
    this.tray = options.createTray()
    // Windows opens the context menu on right click; a left click brings the window back.
    this.tray.on("click", () => this.showWindow())
    this.tray.on("double-click", () => this.showWindow())
    this.render()
  }

  /** Short runtime state shown in the icon tooltip, e.g. "本机服务已就绪". */
  setStatus(status: string): void {
    if (status === this.status) return
    this.status = status
    this.render()
  }

  /** While an approved quit drains the runtime the menu cannot start a second one. */
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
      { type: "separator" },
      {
        label: "登录 Windows 时在后台启动",
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
    this.tray.setToolTip(`${TRAY_TITLE} — ${this.quitting ? "正在安全退出…" : this.status}`)
    this.tray.setContextMenu(this.options.buildMenu(this.menuTemplate()))
  }
}

/** Tooltip wording for a runtime phase; unknown phases never read as healthy. */
export function trayStatusFor(phase: string): string {
  switch (phase) {
    case "ready": return "本机服务已就绪"
    case "degraded": return "本机服务已就绪（远程能力受限）"
    case "starting": return "正在启动本机服务…"
    case "stopped": return "本机服务未运行"
    default: return "本机服务状态未知"
  }
}
