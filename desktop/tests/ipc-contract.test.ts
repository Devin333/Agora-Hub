import path from "node:path"
import { pathToFileURL } from "node:url"
import { describe, expect, it } from "vitest"
import { APP_CHANNELS, CONNECTION_CHANNELS, HOST_EVENTS } from "../src/channels"
import { isTrustedIpcSource } from "../src/ipc"

const origin = "https://agora.example.com"
const connectionFile = path.resolve("out/connect.html")
const connectionUrl = pathToFileURL(connectionFile).href
const frame = (url: string, parent: unknown = null) => ({ senderFrame: { url, parent } })

describe("preload contract", () => {
  it("contains only the enumerated workspace and connection channels", () => {
    expect(Object.values(APP_CHANNELS).sort()).toEqual(["agora:appearance:set", "agora:export:save", "agora:lifecycle:respond", "agora:lifecycle:update"])
    expect(Object.values(CONNECTION_CHANNELS).sort()).toEqual([
      "agora:connection:cancel-edit", "agora:connection:edit", "agora:connection:reset", "agora:connection:retry", "agora:connection:save", "agora:connection:status"
    ])
    expect(Object.values(HOST_EVENTS).sort()).toEqual(["agora:connection:changed", "agora:lifecycle:quit-requested"])
    const all = [...Object.values(APP_CHANNELS), ...Object.values(CONNECTION_CHANNELS), ...Object.values(HOST_EVENTS)]
    expect(new Set(all).size).toBe(all.length)
    expect(all.filter(channel => /shell|exec|read-file|write-file|path|runtime|credential/i.test(channel))).toEqual([])
  })
})

describe("IPC source checks", () => {
  it("lets only workspace pages of the connected server use workspace channels", () => {
    expect(isTrustedIpcSource(frame(`${origin}/paper-agent`), "workspace", origin, connectionFile)).toBe(true)
    expect(isTrustedIpcSource(frame(`${origin}/login`), "workspace", origin, connectionFile)).toBe(true)
    expect(isTrustedIpcSource(frame(`${origin}/admin`), "workspace", origin, connectionFile)).toBe(false)
    expect(isTrustedIpcSource(frame("https://evil.example/paper-agent"), "workspace", origin, connectionFile)).toBe(false)
    expect(isTrustedIpcSource(frame(`${origin}/paper-agent`), "workspace", null, connectionFile)).toBe(false)
    expect(isTrustedIpcSource(frame(connectionUrl), "workspace", origin, connectionFile)).toBe(false)
  })

  it("lets only the packaged connection page use connection channels", () => {
    expect(isTrustedIpcSource(frame(connectionUrl), "connection", null, connectionFile)).toBe(true)
    expect(isTrustedIpcSource(frame(pathToFileURL(path.resolve("out/other.html")).href), "connection", null, connectionFile)).toBe(false)
    expect(isTrustedIpcSource(frame(`${origin}/paper-agent`), "connection", origin, connectionFile)).toBe(false)
  })

  it("refuses subframes and frames that no longer exist", () => {
    expect(isTrustedIpcSource(frame(`${origin}/paper-agent`, {}), "workspace", origin, connectionFile)).toBe(false)
    expect(isTrustedIpcSource(frame(connectionUrl, {}), "connection", null, connectionFile)).toBe(false)
    expect(isTrustedIpcSource({ senderFrame: null }, "connection", null, connectionFile)).toBe(false)
  })
})
