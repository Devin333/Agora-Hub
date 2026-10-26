import path from "node:path"
import type { BrowserWindowConstructorOptions, Rectangle } from "electron"

const DESIRED_WIDTH = 1440
const DESIRED_HEIGHT = 920
const DESIRED_MIN_WIDTH = 1024
const DESIRED_MIN_HEIGHT = 680

export interface WindowGeometry {
  x: number
  y: number
  width: number
  height: number
  minWidth: number
  minHeight: number
}

function positiveInteger(value: number): number {
  return Math.max(1, Math.floor(Number.isFinite(value) ? value : 1))
}

export function computeWindowGeometry(workArea: Rectangle): WindowGeometry {
  const availableWidth = positiveInteger(workArea.width)
  const availableHeight = positiveInteger(workArea.height)
  const width = Math.min(DESIRED_WIDTH, availableWidth)
  const height = Math.min(DESIRED_HEIGHT, availableHeight)
  const minWidth = Math.min(DESIRED_MIN_WIDTH, Math.max(320, Math.floor(availableWidth * 0.72)), availableWidth)
  const minHeight = Math.min(DESIRED_MIN_HEIGHT, Math.max(280, Math.floor(availableHeight * 0.72)), availableHeight)
  const x = Math.floor(workArea.x + (availableWidth - width) / 2)
  const y = Math.floor(workArea.y + (availableHeight - height) / 2)
  return { x, y, width, height, minWidth, minHeight }
}

export function secureWindowOptions(outputDirectory: string, workArea: Rectangle): BrowserWindowConstructorOptions {
  return {
    ...computeWindowGeometry(workArea),
    show: false,
    backgroundColor: "#fcfcfc",
    title: "Agora Paper Agent",
    autoHideMenuBar: true,
    icon: path.join(outputDirectory, "icon.png"),
    webPreferences: {
      preload: path.join(outputDirectory, "preload.cjs"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      spellcheck: true
    }
  }
}
