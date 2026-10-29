import { describe, expect, it, vi } from "vitest"
import type { MenuItemConstructorOptions } from "electron"
import { BACKGROUND_LAUNCH_ARGUMENT, BackgroundTray, isBackgroundLaunch, LaunchAtLogin, launchAtLoginLabel, trayStatusFor, type TrayLike } from "../src/background-host"

class FakeTray implements TrayLike {
  toolTip = ""
  menu: MenuItemConstructorOptions[] = []
  destroyed = false
  readonly listeners = new Map<string, () => void>()
  setToolTip(toolTip: string): void { this.toolTip = toolTip }
  setContextMenu(menu: unknown): void { this.menu = menu as MenuItemConstructorOptions[] }
  on(event: "click" | "double-click", listener: () => void): this {
    this.listeners.set(event, listener)
    return this
  }
  destroy(): void { this.destroyed = true }
  isDestroyed(): boolean { return this.destroyed }
}

function loginApplication(isPackaged: boolean, openAtLogin = false) {
  const state = { openAtLogin }
  return {
    state,
    isPackaged,
    getLoginItemSettings: vi.fn((_options: { args: string[] }) => ({ openAtLogin: state.openAtLogin })),
    setLoginItemSettings: vi.fn((settings: { openAtLogin: boolean; args: string[] }) => { state.openAtLogin = settings.openAtLogin })
  }
}

function trayHarness(options: { packaged?: boolean; openAtLogin?: boolean } = {}) {
  const tray = new FakeTray()
  const application = loginApplication(options.packaged ?? true, options.openAtLogin ?? false)
  const actions = { showWindow: vi.fn(), changeConnection: vi.fn(), stopLocalStack: vi.fn(), showLocalStackLogs: vi.fn(), quit: vi.fn() }
  const onLaunchAtLoginError = vi.fn()
  const background = new BackgroundTray({
    createTray: () => tray,
    buildMenu: template => template,
    launchAtLogin: new LaunchAtLogin(application),
    actions,
    onLaunchAtLoginError,
    platform: "win32"
  })
  const item = (label: string) => {
    const found = tray.menu.find(entry => entry.label === label)
    if (!found) throw new Error(`menu item missing: ${label}`)
    return found
  }
  const click = (label: string) => (item(label).click as (entry: { checked?: boolean }) => void)({ checked: !item(label).checked })
  return { tray, application, actions, background, item, click, onLaunchAtLoginError }
}

describe("background launch argument", () => {
  it("detects the login-item launch only by its exact argument", () => {
    expect(isBackgroundLaunch(["electron.exe", ".", BACKGROUND_LAUNCH_ARGUMENT])).toBe(true)
    expect(isBackgroundLaunch(["electron.exe", ".", "--agora-background=1"])).toBe(false)
    expect(isBackgroundLaunch(["electron.exe", "."])).toBe(false)
  })
})

describe("launch at login", () => {
  it("registers and reads the Run entry with the background argument", () => {
    const application = loginApplication(true)
    const launchAtLogin = new LaunchAtLogin(application)
    launchAtLogin.set(true)
    expect(application.setLoginItemSettings).toHaveBeenCalledWith({ openAtLogin: true, args: [BACKGROUND_LAUNCH_ARGUMENT] })
    expect(launchAtLogin.enabled).toBe(true)
    expect(application.getLoginItemSettings).toHaveBeenCalledWith({ args: [BACKGROUND_LAUNCH_ARGUMENT] })
  })

  it("is unavailable and inert for an unpackaged host", () => {
    const application = loginApplication(false, true)
    const launchAtLogin = new LaunchAtLogin(application)
    launchAtLogin.set(true)
    expect(launchAtLogin.available).toBe(false)
    expect(launchAtLogin.enabled).toBe(false)
    expect(application.setLoginItemSettings).not.toHaveBeenCalled()
    expect(application.getLoginItemSettings).not.toHaveBeenCalled()
  })
})

describe("background tray", () => {
  it("brings the window back on click and quits only through the explicit menu item", () => {
    const { tray, actions, click } = trayHarness()
    tray.listeners.get("click")?.()
    tray.listeners.get("double-click")?.()
    click("显示 Agora Paper Agent")
    expect(actions.showWindow).toHaveBeenCalledTimes(3)
    expect(actions.quit).not.toHaveBeenCalled()
    click("退出")
    expect(actions.quit).toHaveBeenCalledOnce()
  })

  it("offers switching the connection without showing or quitting", () => {
    const { actions, click } = trayHarness()
    click("切换连接方式…")
    expect(actions.changeConnection).toHaveBeenCalledOnce()
    expect(actions.showWindow).not.toHaveBeenCalled()
    expect(actions.quit).not.toHaveBeenCalled()
  })

  it("offers stopping the local stack and its logs only in local mode", () => {
    const { tray, actions, background, click } = trayHarness()
    const labels = () => tray.menu.map(entry => entry.label).filter(Boolean)
    expect(labels()).not.toContain("停止本机服务")
    background.setLocalMode(true)
    expect(labels()).toEqual(["显示 Agora Paper Agent", "切换连接方式…", "停止本机服务", "查看本机服务日志", "登录 Windows 时在后台启动", "退出"])
    click("停止本机服务")
    click("查看本机服务日志")
    expect(actions.stopLocalStack).toHaveBeenCalledOnce()
    expect(actions.showLocalStackLogs).toHaveBeenCalledOnce()
    background.setLocalMode(false)
    expect(labels()).not.toContain("查看本机服务日志")
  })

  it("reflects the connection in the tooltip", () => {
    const { tray, background } = trayHarness()
    expect(tray.toolTip).toBe("Agora Paper Agent — 正在连接服务器…")
    background.setStatus(trayStatusFor("connected", "https://agora.example.com"))
    expect(tray.toolTip).toBe("Agora Paper Agent — 已连接 agora.example.com")
  })

  it("locks the menu while an approved quit leaves and unlocks it if the exit is abandoned", () => {
    const { tray, actions, background, item, click } = trayHarness()
    background.setQuitting(true)
    expect(tray.toolTip).toBe("Agora Paper Agent — 正在退出…")
    expect(item("正在退出…").enabled).toBe(false)
    expect(item("显示 Agora Paper Agent").enabled).toBe(false)
    expect(item("切换连接方式…").enabled).toBe(false)
    tray.listeners.get("click")?.()
    expect(actions.showWindow).not.toHaveBeenCalled()
    background.setQuitting(false)
    expect(item("退出").enabled).toBe(true)
    click("显示 Agora Paper Agent")
    expect(actions.showWindow).toHaveBeenCalledOnce()
  })

  it("toggles launch at login and shows the state the OS actually holds", () => {
    const { application, item, click } = trayHarness()
    expect(item("登录 Windows 时在后台启动").checked).toBe(false)
    click("登录 Windows 时在后台启动")
    expect(application.state.openAtLogin).toBe(true)
    expect(item("登录 Windows 时在后台启动").checked).toBe(true)
  })

  it("keeps the checkbox truthful when the OS rejects the change", () => {
    const { application, item, click, onLaunchAtLoginError } = trayHarness()
    application.setLoginItemSettings.mockImplementation(() => { throw new Error("registry denied") })
    click("登录 Windows 时在后台启动")
    expect(onLaunchAtLoginError).toHaveBeenCalledOnce()
    expect(item("登录 Windows 时在后台启动").checked).toBe(false)
  })

  it("disables the launch-at-login switch for an unpackaged host", () => {
    const { item } = trayHarness({ packaged: false })
    expect(item("登录 Windows 时在后台启动").enabled).toBe(false)
  })

  it("names the system the app runs on", () => {
    expect(launchAtLoginLabel("darwin")).toBe("登录 Mac 时在后台启动")
    expect(launchAtLoginLabel("linux")).toBe("登录系统时在后台启动")
  })

  it("destroys the icon once", () => {
    const { tray, background } = trayHarness()
    background.destroy()
    background.destroy()
    expect(tray.destroyed).toBe(true)
  })

  it("never words a phase other than connected as healthy", () => {
    expect(trayStatusFor("connected", "http://127.0.0.1:3317")).toBe("已连接 127.0.0.1:3317")
    expect(trayStatusFor("failed", "https://agora.example.com")).toBe("无法连接服务器")
    expect(trayStatusFor("unconfigured", null)).toBe("尚未选择连接方式")
    expect(trayStatusFor("editing", "https://agora.example.com")).toBe("正在切换连接方式")
    expect(trayStatusFor("connected", "http://127.0.0.1:45180", "local")).toBe("已连接本机服务")
    expect(trayStatusFor("connecting", null, "local")).toBe("正在启动本机服务…")
    expect(trayStatusFor("failed", null, "local")).toBe("本机服务不可用")
    expect(trayStatusFor("local-setup", "http://127.0.0.1:45180", "local")).toBe("等待设置本机管理员")
  })
})
