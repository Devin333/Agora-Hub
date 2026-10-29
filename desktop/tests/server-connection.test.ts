import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import { BUNDLED_SERVER_SCHEMA, bundledServerFilePath, connectionFilePath, readBundledServerOrigin, ServerConnectionStore } from "../src/server-connection"

const packaged = { allowLoopbackHttp: false }
let directory: string
let connectionFile: string

beforeEach(() => {
  directory = mkdtempSync(path.join(os.tmpdir(), "agora-connection-"))
  connectionFile = connectionFilePath(directory)
})
afterEach(() => rmSync(directory, { recursive: true, force: true }))

function seedConnection(contents: string) {
  mkdirSync(path.dirname(connectionFile), { recursive: true })
  writeFileSync(connectionFile, contents)
}

describe("bundled server address", () => {
  it("reads the build's origin and treats a missing, null or invalid one as absent", () => {
    const file = bundledServerFilePath(directory)
    expect(readBundledServerOrigin(file, packaged)).toBeNull()
    writeFileSync(file, JSON.stringify({ schema: BUNDLED_SERVER_SCHEMA, origin: "https://agora.example.com" }))
    expect(readBundledServerOrigin(file, packaged)).toBe("https://agora.example.com")
    writeFileSync(file, JSON.stringify({ schema: BUNDLED_SERVER_SCHEMA, origin: null }))
    expect(readBundledServerOrigin(file, packaged)).toBeNull()
    writeFileSync(file, JSON.stringify({ schema: BUNDLED_SERVER_SCHEMA, origin: "http://agora.example.com" }))
    expect(readBundledServerOrigin(file, packaged)).toBeNull()
    writeFileSync(file, JSON.stringify({ schema: "other", origin: "https://agora.example.com" }))
    expect(readBundledServerOrigin(file, packaged)).toBeNull()
    writeFileSync(file, "{")
    expect(readBundledServerOrigin(file, packaged)).toBeNull()
  })
})

describe("server connection store", () => {
  it("prefers the user's address over the build's and falls back to asking", () => {
    expect(new ServerConnectionStore({ connectionFile, bundledOrigin: null, policy: packaged }).view).toEqual({ origin: null, source: null, bundledOrigin: null })
    expect(new ServerConnectionStore({ connectionFile, bundledOrigin: "https://default.example.com", policy: packaged }).view)
      .toEqual({ origin: "https://default.example.com", source: "bundled", bundledOrigin: "https://default.example.com" })
    seedConnection(JSON.stringify({ schema: "agora.desktop-connection/v1", origin: "https://team.example.com" }))
    expect(new ServerConnectionStore({ connectionFile, bundledOrigin: "https://default.example.com", policy: packaged }).view)
      .toEqual({ origin: "https://team.example.com", source: "user", bundledOrigin: "https://default.example.com" })
  })

  it("ignores a stored address the current policy would refuse", () => {
    seedConnection(JSON.stringify({ schema: "agora.desktop-connection/v1", origin: "http://127.0.0.1:3317" }))
    expect(new ServerConnectionStore({ connectionFile, bundledOrigin: null, policy: packaged }).view.origin).toBeNull()
    expect(new ServerConnectionStore({ connectionFile, bundledOrigin: null, policy: { allowLoopbackHttp: true } }).view.origin).toBe("http://127.0.0.1:3317")
    seedConnection(JSON.stringify({ schema: "agora.desktop-connection/v1", origin: "https://team.example.com/paper-agent" }))
    expect(new ServerConnectionStore({ connectionFile, bundledOrigin: null, policy: packaged }).view.origin).toBeNull()
  })

  it("saves atomically, survives a restart and resets back to the build's address", async () => {
    const store = new ServerConnectionStore({ connectionFile, bundledOrigin: "https://default.example.com", policy: packaged })
    await expect(store.save("https://team.example.com")).resolves.toMatchObject({ origin: "https://team.example.com", source: "user" })
    expect(JSON.parse(readFileSync(connectionFile, "utf8"))).toEqual({ schema: "agora.desktop-connection/v1", origin: "https://team.example.com" })
    expect(new ServerConnectionStore({ connectionFile, bundledOrigin: "https://default.example.com", policy: packaged }).view.source).toBe("user")
    await expect(store.reset()).resolves.toMatchObject({ origin: "https://default.example.com", source: "bundled" })
    expect(existsSync(connectionFile)).toBe(false)
  })

  it("stores no copy of the build's own address", async () => {
    const store = new ServerConnectionStore({ connectionFile, bundledOrigin: "https://default.example.com", policy: packaged })
    await store.save("https://team.example.com")
    await expect(store.save("https://default.example.com")).resolves.toMatchObject({ source: "bundled" })
    expect(existsSync(connectionFile)).toBe(false)
  })

  it("refuses to persist an address that is not a valid origin", async () => {
    const store = new ServerConnectionStore({ connectionFile, bundledOrigin: null, policy: packaged })
    await expect(store.save("http://team.example.com")).rejects.toThrow("server_url_insecure")
    expect(existsSync(connectionFile)).toBe(false)
  })
})
