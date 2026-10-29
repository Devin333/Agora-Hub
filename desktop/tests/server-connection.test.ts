import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { afterEach, beforeEach, describe, expect, it } from "vitest"
import { BUILD_CONFIG_SCHEMA, buildConfigFilePath, developmentImageOverride, readBuildConfig } from "../src/build-config"
import { CONNECTION_SCHEMA, connectionFilePath, ServerConnectionStore } from "../src/server-connection"

const packaged = { allowLoopbackHttp: false }
const OFFICIAL = "https://agora.example.com"
let directory: string
let connectionFile: string

beforeEach(() => {
  directory = mkdtempSync(path.join(os.tmpdir(), "agora-connection-"))
  connectionFile = connectionFilePath(directory)
})
afterEach(() => rmSync(directory, { recursive: true, force: true }))

function seedConnection(document: unknown) {
  mkdirSync(path.dirname(connectionFile), { recursive: true })
  writeFileSync(connectionFile, typeof document === "string" ? document : JSON.stringify(document))
}

function store(officialOrigin: string | null = OFFICIAL, policy = packaged) {
  return new ServerConnectionStore({ connectionFile, officialOrigin, policy })
}

describe("build configuration", () => {
  it("reads the official service and the image release", () => {
    const file = buildConfigFilePath(directory)
    expect(readBuildConfig(file, packaged)).toEqual({ officialOrigin: null, imageTag: null })
    writeFileSync(file, JSON.stringify({ schema: BUILD_CONFIG_SCHEMA, officialOrigin: OFFICIAL, imageTag: "v0.1.2" }))
    expect(readBuildConfig(file, packaged)).toEqual({ officialOrigin: OFFICIAL, imageTag: "v0.1.2" })
  })

  it("lets a development host run images from another registry and release", () => {
    expect(developmentImageOverride({ AGORA_DESKTOP_IMAGE_TAG: "check", AGORA_DESKTOP_IMAGE_REGISTRY: "registry.cn-hangzhou.aliyuncs.com/agora-hub" }))
      .toEqual({ imageTag: "check", imageRegistry: "registry.cn-hangzhou.aliyuncs.com/agora-hub" })
    expect(developmentImageOverride({ AGORA_DESKTOP_IMAGE_TAG: "v1; rm", AGORA_DESKTOP_IMAGE_REGISTRY: "Bad Registry" })).toEqual({ imageTag: null, imageRegistry: null })
    expect(developmentImageOverride({})).toEqual({ imageTag: null, imageRegistry: null })
  })

  it("drops each value the host would refuse, keeping the others", () => {
    const file = buildConfigFilePath(directory)
    writeFileSync(file, JSON.stringify({ schema: BUILD_CONFIG_SCHEMA, officialOrigin: "http://agora.example.com", imageTag: "v1 && rm" }))
    expect(readBuildConfig(file, packaged)).toEqual({ officialOrigin: null, imageTag: null })
    writeFileSync(file, JSON.stringify({ schema: BUILD_CONFIG_SCHEMA, officialOrigin: null, imageTag: "edge" }))
    expect(readBuildConfig(file, packaged)).toEqual({ officialOrigin: null, imageTag: "edge" })
    writeFileSync(file, JSON.stringify({ schema: "other", officialOrigin: OFFICIAL }))
    expect(readBuildConfig(file, packaged).officialOrigin).toBeNull()
    writeFileSync(file, "{")
    expect(readBuildConfig(file, packaged).officialOrigin).toBeNull()
  })
})

describe("connection store", () => {
  it("opens the official service by default only when the build has one", () => {
    expect(store().view).toEqual({ mode: "official", serverOrigin: null, officialOrigin: OFFICIAL })
    expect(store(null).view).toEqual({ mode: null, serverOrigin: null, officialOrigin: null })
  })

  it("prefers the user's saved choice and keeps the last server address across modes", async () => {
    const connection = store()
    await expect(connection.saveServer("https://team.example.com")).resolves.toEqual({ mode: "server", serverOrigin: "https://team.example.com", officialOrigin: OFFICIAL })
    await expect(connection.saveLocal()).resolves.toMatchObject({ mode: "local", serverOrigin: "https://team.example.com" })
    expect(JSON.parse(readFileSync(connectionFile, "utf8"))).toEqual({ schema: CONNECTION_SCHEMA, mode: "local", serverOrigin: "https://team.example.com" })
    expect(store().view).toMatchObject({ mode: "local", serverOrigin: "https://team.example.com" })
    await expect(connection.saveOfficial()).resolves.toMatchObject({ mode: "official" })
    expect(store().view.mode).toBe("official")
  })

  it("refuses the official choice in a build without one, and forgets it when a later build drops it", async () => {
    await expect(store(null).saveOfficial()).rejects.toThrow("official_service_unavailable")
    seedConnection({ schema: CONNECTION_SCHEMA, mode: "official", serverOrigin: null })
    expect(store(null).view.mode).toBeNull()
  })

  it("migrates an address saved by the previous version", () => {
    seedConnection({ schema: "agora.desktop-connection/v1", origin: "https://team.example.com" })
    expect(store().view).toMatchObject({ mode: "server", serverOrigin: "https://team.example.com" })
    seedConnection({ schema: "agora.desktop-connection/v1", origin: OFFICIAL })
    expect(store().view).toMatchObject({ mode: "official", serverOrigin: null })
  })

  it("ignores a stored address or mode the current policy would refuse", () => {
    seedConnection({ schema: CONNECTION_SCHEMA, mode: "server", serverOrigin: "http://127.0.0.1:3317" })
    expect(store(null).view).toMatchObject({ mode: null, serverOrigin: null })
    expect(store(null, { allowLoopbackHttp: true }).view).toMatchObject({ mode: "server", serverOrigin: "http://127.0.0.1:3317" })
    seedConnection({ schema: CONNECTION_SCHEMA, mode: "server", serverOrigin: "https://team.example.com/paper-agent" })
    expect(store(null).view.mode).toBeNull()
    seedConnection({ schema: CONNECTION_SCHEMA, mode: "somewhere", serverOrigin: null })
    expect(store(null).view.mode).toBeNull()
    seedConnection("{")
    expect(store(null).view.mode).toBeNull()
  })

  it("refuses to persist an address that is not a valid origin", async () => {
    await expect(store(null).saveServer("http://team.example.com")).rejects.toThrow("server_url_insecure")
    expect(store(null).view.mode).toBeNull()
  })
})
