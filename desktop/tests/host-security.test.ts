import { describe, expect, it, vi } from "vitest"
import { computeWindowGeometry, secureWindowOptions } from "../src/window-options"
import { acquireSingleInstanceLock } from "../src/single-instance"

function expectInsideWorkArea(workArea: { x: number; y: number; width: number; height: number }) {
  const geometry = computeWindowGeometry(workArea)
  expect(geometry.minWidth).toBeLessThanOrEqual(workArea.width)
  expect(geometry.minHeight).toBeLessThanOrEqual(workArea.height)
  expect(geometry.width).toBeLessThanOrEqual(workArea.width)
  expect(geometry.height).toBeLessThanOrEqual(workArea.height)
  expect(geometry.x).toBeGreaterThanOrEqual(workArea.x)
  expect(geometry.y).toBeGreaterThanOrEqual(workArea.y)
  expect(geometry.x + geometry.width).toBeLessThanOrEqual(workArea.x + workArea.width)
  expect(geometry.y + geometry.height).toBeLessThanOrEqual(workArea.y + workArea.height)
  return geometry
}

describe("desktop host security configuration", () => {
  it("enforces Chromium isolation and keeps native window chrome", () => {
    const options = secureWindowOptions("C:\\host\\out", { x: 0, y: 0, width: 1920, height: 1040 }, "dark")
    expect(options.frame).not.toBe(false)
    expect(options.titleBarStyle).toBeUndefined()
    expect(options.webPreferences).toMatchObject({
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false
    })
  })

  it("paints the frame with the workspace background of the resolved theme before any page loads", () => {
    const workArea = { x: 0, y: 0, width: 1920, height: 1040 }
    expect(secureWindowOptions("C:\\host\\out", workArea, "dark").backgroundColor).toBe("#161616")
    expect(secureWindowOptions("C:\\host\\out", workArea, "light").backgroundColor).toBe("#ffffff")
  })

  it("clamps a 1366x768 display at 125 percent scaling to the 1092x584 DIP work area", () => {
    expect(expectInsideWorkArea({ x: 0, y: 0, width: 1092, height: 584 })).toMatchObject({ width: 1092, height: 584 })
  })

  it("centers the initial window on the selected external display", () => {
    const geometry = expectInsideWorkArea({ x: 1920, y: -120, width: 1920, height: 1040 })
    expect(geometry).toMatchObject({ x: 2160, y: -60, width: 1440, height: 920 })
  })

  it("never sets a hard minimum larger than a very small work area", () => {
    expect(expectInsideWorkArea({ x: -480, y: 80, width: 280, height: 220 })).toMatchObject({ width: 280, height: 220, minWidth: 280, minHeight: 220 })
  })

  it("delegates duplicate prevention to Electron's process lock", () => {
    const requestSingleInstanceLock = vi.fn(() => true)
    expect(acquireSingleInstanceLock({ requestSingleInstanceLock })).toBe(true)
    expect(requestSingleInstanceLock).toHaveBeenCalledOnce()
  })
})
