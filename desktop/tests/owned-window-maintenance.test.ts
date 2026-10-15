import { describe, expect, it } from "vitest"
import { IPC_CHANNELS } from "../src/channels"
import { OwnedWindowMaintenanceFence } from "../src/owned-window-maintenance"

function event(processId = 11, routingId = 22, webContentsId = 33) {
  return {
    sender: { id: webContentsId },
    senderFrame: { processId, routingId, isDestroyed: () => false }
  } as never
}

describe("OwnedWindowMaintenanceFence", () => {
  it("authorizes only the main-owned document identity and revokes it on navigation", () => {
    const fence = new OwnedWindowMaintenanceFence()
    const original = event()
    fence.authorizeDocument(33, 11, 22)
    expect(fence.isAuthorizedDocument(original)).toBe(true)
    expect(fence.isAuthorizedDocument(event(11, 23, 33))).toBe(false)
    fence.revokeDocument(34)
    expect(fence.isAuthorizedDocument(original)).toBe(true)
    fence.revokeDocument(33)
    expect(fence.isAuthorizedDocument(original)).toBe(false)
  })
  it("pins maintenance IPC to the exact existing renderer document", () => {
    const fence = new OwnedWindowMaintenanceFence()
    const original = event()
    expect(fence.enter(original)).toBe(true)
    expect(fence.allowsIpc(original, IPC_CHANNELS.runtimeClearDataApply)).toBe(true)
    expect(fence.allowsIpc(original, IPC_CHANNELS.lifecycleRespond)).toBe(true)
    expect(fence.allowsIpc(original, IPC_CHANNELS.runtimeBackup)).toBe(false)
    expect(fence.allowsIpc(event(12, 22, 33), IPC_CHANNELS.runtimeClearDataStatus)).toBe(false)
    expect(fence.allowsIpc(event(11, 23, 33), IPC_CHANNELS.runtimeClearDataStatus)).toBe(false)
    expect(fence.allowsIpc(event(11, 22, 34), IPC_CHANNELS.runtimeClearDataStatus)).toBe(false)
  })

  it("blocks reload/navigation and network only for the owned maintenance window", () => {
    const fence = new OwnedWindowMaintenanceFence()
    fence.enter(event())
    expect(fence.blocksNavigation(33)).toBe(true)
    expect(fence.blocksNavigation(34)).toBe(false)
    expect(fence.blocksInputReload(33, "F5", false, false, false)).toBe(true)
    expect(fence.blocksInputReload(33, "r", true, false, false)).toBe(true)
    expect(fence.blocksRequest({ webContentsId: 33, url: "http://127.0.0.1:3120/paper-agent" })).toBe(true)
    expect(fence.blocksRequest({ webContentsId: 33, url: "wss://127.0.0.1:3120/socket" })).toBe(true)
    expect(fence.blocksRequest({ webContentsId: 34, url: "http://127.0.0.1:3120/paper-agent" })).toBe(false)
    expect(fence.blocksRequest({ webContentsId: 33, url: "file:///C:/Agora/startup.html" })).toBe(false)
  })
})

