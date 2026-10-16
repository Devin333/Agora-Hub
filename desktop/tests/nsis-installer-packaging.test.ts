import { createHash } from "node:crypto"
import { execFile, type ChildProcess, type SpawnOptions } from "node:child_process"
import { copyFile, link, mkdir, mkdtemp, readFile, readdir, rename, rm, stat, symlink, writeFile } from "node:fs/promises"
import { createRequire } from "node:module"
import os from "node:os"
import path from "node:path"
import { promisify } from "node:util"
import { compileFunction } from "node:vm"
import { afterEach, describe, expect, it } from "vitest"

const exec = promisify(execFile)
const desktopDir = path.resolve(__dirname, "..")
const bootstrap = path.join(desktopDir, "assets/nsis/installer-bootstrap.cjs")
const packaging = require("../scripts/nsis-installer-packaging.cjs") as {
  EXTRACTOR_SHA256: string
  TEMPLATE_LOCK: Record<string, { size: number; sha256: string }>
  authorPayloadManifest(options: Record<string, unknown>): Promise<Buffer>
  cleanupOwnedGeneratedDirectory(token: any): Promise<void>
  createNsisInstallerPackaging(options: Record<string, unknown>): any
  createOwnedGeneratedDirectory(outputParent: string): Promise<any>
  resolvePinnedExtractor(): Promise<{ extractor: string; distribution: string }>
  verifyArchiveInventory(options: { extractor: string; archive: string; manifestBytes: Buffer }): Promise<void>
  verifyTemplates(projectDir: string): Promise<string>
  writeGeneratedInputs(options: Record<string, unknown>): Promise<any>
}

const cleanup: string[] = []
const blockedEnv = new Set([
  "AGORA_NSIS_EXTRACTOR", "COMSPEC", "NODE_OPTIONS", "NODE_PATH", "NODE_REPL_EXTERNAL_MODULE",
  "NODE_EXTRA_CA_CERTS", "NODE_ICU_DATA", "NODE_V8_COVERAGE", "OPENSSL_CONF", "OPENSSL_MODULES",
  "SSL_CERT_DIR", "SSL_CERT_FILE", "PATH", "PATHEXT"
])
function native(value: string) { return process.platform === "win32" ? path.toNamespacedPath(value) : value }
function digest(bytes: Buffer | string, algorithm: "sha256" | "sha512" = "sha256") { return createHash(algorithm).update(bytes).digest("hex") }
async function fileDigest(file: string, algorithm: "sha256" | "sha512" = "sha256") { return digest(await readFile(native(file)), algorithm) }
function cleanEnvironment(extra: NodeJS.ProcessEnv = {}) {
  const env: NodeJS.ProcessEnv = { ...process.env }
  for (const name of Object.keys(env)) if (blockedEnv.has(name.toUpperCase()) || name.toUpperCase().startsWith("AGORA_NSIS_")) delete env[name]
  for (const name of blockedEnv) env[name] = ""
  return { ...env, ...extra }
}
async function temp(prefix: string) { const root = await mkdtemp(path.join(os.tmpdir(), prefix)); cleanup.push(root); return root }
async function runBootstrap(args: string[], env: NodeJS.ProcessEnv = cleanEnvironment()) {
  try {
    return await exec(process.execPath, [bootstrap, ...args], { env, timeout: 60_000, windowsHide: true, maxBuffer: 2 * 1024 * 1024 })
  } catch (error) {
    const resultPath = args[args.length - 1]
    try { (error as Error).message += "\nbootstrap-result=" + await readFile(resultPath, "utf8") } catch {}
    throw error
  }
}
async function archiveDirectory(extractor: string, source: string, archive: string) {
  await exec(extractor, ["a", "-t7z", "-mx=1", "-m0=LZMA2", "-mf=off", archive, "*"], { cwd: source, windowsHide: true, env: {} })
}

type Fixture = Awaited<ReturnType<typeof payloadFixture>>
async function payloadFixture(version: string) {
  const root = await temp(`agora-nsis-${version}-`)
  const appOutDir = path.join(root, "app")
  const runtime = path.join(appOutDir, "resources", "runtime")
  const longParts: string[] = []
  while (path.join(runtime, "licenses", ...longParts, "license.txt").length < 285) longParts.push(`segment-${"x".repeat(28)}`)
  const resources = [
    { path: "node/node.exe", bytes: Buffer.from(`node-${version}`) },
    { path: `licenses/${longParts.join("/")}/license.txt`, bytes: Buffer.from(`license-${version}`) }
  ]
  for (const item of resources) {
    const file = path.join(runtime, ...item.path.split("/")); await mkdir(native(path.dirname(file)), { recursive: true }); await writeFile(native(file), item.bytes)
  }
  const runtimeManifest = {
    schema: "newsroom.desktop-app-bundle/v1", bundle_id: `fixture-${version}`,
    resources: resources.map(item => ({ path: item.path, size: item.bytes.length, sha256: digest(item.bytes) }))
  }
  await writeFile(native(path.join(runtime, "desktop-bundle.json")), JSON.stringify(runtimeManifest))
  await writeFile(native(path.join(appOutDir, "Agora.exe")), `electron-${version}`)
  const tool = await packaging.resolvePinnedExtractor()
  const archive = path.join(root, `payload-${version}.7z`)
  await archiveDirectory(tool.extractor, appOutDir, archive)
  const archiveSha512 = await fileDigest(archive, "sha512")
  const manifestBytes = await packaging.authorPayloadManifest({
    appOutDir, archiveSha512, productId: "com.agorahub.fixture", uninstallerFile: "Uninstall Fixture.exe", preFiles: []
  })
  const manifest = path.join(root, `payload-${version}.json`); await writeFile(manifest, manifestBytes)
  return { root, appOutDir, runtime, archive, archiveSha512, manifest, manifestSha512: digest(manifestBytes, "sha512"), tool, resources }
}
function installArgs(mode: "preflight" | "install", fixture: Fixture, installRoot: string, result: string) {
  return [mode, fixture.tool.extractor, packaging.EXTRACTOR_SHA256, fixture.archive, fixture.archiveSha512, fixture.manifest, fixture.manifestSha512, installRoot, "com.agorahub.fixture", result]
}
async function seal(fixture: Fixture, installRoot: string) {
  const uninstaller = path.join(installRoot, "Uninstall Fixture.exe")
  await writeFile(native(uninstaller), `uninstaller-${path.basename(fixture.root)}`)
  await runBootstrap(["seal", fixture.manifest, fixture.manifestSha512, installRoot, "com.agorahub.fixture", path.join(fixture.root, "seal.json")])
  const self = path.join(fixture.root, "running-uninstaller.exe"); await copyFile(native(uninstaller), native(self))
  return self
}
async function installAndSeal(fixture: Fixture, installRoot: string) {
  await runBootstrap(installArgs("install", fixture, installRoot, path.join(fixture.root, "install.json")))
  return seal(fixture, installRoot)
}

afterEach(async () => {
  for (const directory of cleanup.splice(0).reverse()) await rm(native(directory), { recursive: true, force: true })
})

describe("hardened NSIS packaging locks", () => {
  it("pins app-builder templates, package lock, modern extractor and complete licenses", async () => {
    await expect(packaging.verifyTemplates(desktopDir)).resolves.toContain("templates\\nsis")
    const tool = await packaging.resolvePinnedExtractor()
    expect(await stat(tool.extractor)).toMatchObject({ size: 849920 })
    expect(await fileDigest(tool.extractor)).toBe("223b873c50380fe9a39f1a22b6abf8d46db506e1c08d08312902f6f3cd1f7ac3")
    await expect(readFile(path.join(tool.distribution, "COPYING"))).resolves.toHaveLength(27032)
    await expect(readFile(path.join(tool.distribution, "LICENSE.txt"))).resolves.toHaveLength(6341)

    const fake = await temp("agora-template-drift-")
    await mkdir(path.join(fake, "node_modules", "app-builder-lib", "templates", "nsis", "include"), { recursive: true })
    await writeFile(path.join(fake, "node_modules", "app-builder-lib", "package.json"), JSON.stringify({ version: "26.15.3" }))
    await writeFile(path.join(fake, "package-lock.json"), JSON.stringify({ packages: { "node_modules/app-builder-lib": { version: "26.15.3", integrity: "sha512-2VnyWkqsP5v5XbBhL3tD5Syx8iNPBYsoU7kY4S2fz7wg8Rj/nztWKCUzGKaFRTv0Xwf3/H058CR1Kvtd/3lRow==" } } }))
    for (const relative of Object.keys(packaging.TEMPLATE_LOCK)) {
      const destination = path.join(fake, "node_modules", "app-builder-lib", "templates", "nsis", ...relative.split("/")); await mkdir(path.dirname(destination), { recursive: true }); await copyFile(path.join(desktopDir, "node_modules", "app-builder-lib", "templates", "nsis", ...relative.split("/")), destination)
    }
    await writeFile(path.join(fake, "node_modules", "app-builder-lib", "templates", "nsis", "installSection.nsh"), "drift")
    await expect(packaging.verifyTemplates(fake)).rejects.toThrow(/mismatch/)
  })

  it("installs the private effective-options hook and authors an independent full payload manifest", async () => {
    const fixture = await payloadFixture("hook")
    const stage = path.join(fixture.root, "stage"); await mkdir(path.join(stage, "node"), { recursive: true }); await writeFile(path.join(stage, "node", "node.exe"), "trusted-stage-node")
    const records = [{ copied: true, appOutDir: fixture.appOutDir, owner: { root: stage } }]
    const hooks = packaging.createNsisInstallerPackaging({ projectDir: desktopDir, runtimeHooks: { getBuildRecords: () => records } })
    const packagerOptions: Record<string, unknown> = {}
    const context = { electronPlatformName: "win32", appOutDir: fixture.appOutDir, packager: { packagerOptions } }
    await hooks.beforePack(context)
    await expect(hooks.afterPack(context)).resolves.toBeUndefined()
    const archive = path.join(fixture.root, "hook-with-licenses.7z"); await archiveDirectory(fixture.tool.extractor, fixture.appOutDir, archive)
    const defines: Record<string, any> = {
      APP_64: archive, APP_64_HASH: (await fileDigest(archive, "sha512")).toUpperCase(), COMPRESSION_METHOD: "7z",
      APP_ID: "com.agorahub.fixture", PRODUCT_FILENAME: "Fixture"
    }
    await (packagerOptions.effectiveOptionComputed as Function)([defines, {}])
    expect(defines.AGORA_NSIS_7ZA_SHA256).toBe(packaging.EXTRACTOR_SHA256)
    const generated = defines.AGORA_NSIS_GENERATED_DIR as string
    const shim = await readFile(path.join(generated, "installer.nsh"), "utf8")
    expect(shim).toContain("AGORA_NSIS_INSTALLER_OVERRIDES_NSH")
    expect(shim.trimEnd().endsWith("Call AgoraPreflightOnce")).toBe(true)
    expect(defines.AGORA_NSIS_INSTALLER_FUNCTIONS_NSH).toBe(path.join(desktopDir, "assets/nsis/installer-functions.nsh"))
    const manifest = JSON.parse(await readFile(defines.AGORA_NSIS_PAYLOAD_MANIFEST_SOURCE, "utf8"))
    expect(manifest.files.map((item: any) => item.path)).toEqual(expect.arrayContaining(["resources/third-party/7zip/COPYING", "resources/third-party/7zip/LICENSE.txt"]))
    expect(manifest.runtime.resourceCount).toBe(2)
    await hooks.afterAllArtifactBuild()
    await expect(stat(generated)).rejects.toMatchObject({ code: "ENOENT" })
  })

  it("keeps sealing inside installApplicationFiles before stock registration and defines no late customInstall", async () => {
    const customization = await readFile(path.join(desktopDir, "assets/nsis/installer-customization.nsh"), "utf8")
    const functions = await readFile(path.join(desktopDir, "assets/nsis/installer-functions.nsh"), "utf8")
    const overrides = await readFile(path.join(desktopDir, "assets/nsis/installer-overrides.nsh"), "utf8")
    expect(customization).not.toContain("!macro customInstall")
    expect(customization).toContain('!include "${AGORA_NSIS_INSTALLER_FUNCTIONS_NSH}"')
    expect(functions).toContain("Function AgoraPreflightOnce")
    expect(functions).toContain("Call AgoraAdmitRegisteredOldInstallations")
    expect(functions.split("nsExec::ExecToStack /TIMEOUT=960000")).toHaveLength(5)
    expect(overrides).toContain("!macroundef installApplicationFiles")
    expect(overrides).not.toContain("Function Agora")
    expect(overrides).not.toContain("!macroundef uninstallOldVersion")
    expect(overrides.indexOf("Call AgoraSealInstallation")).toBeLessThan(overrides.indexOf("!macroundef x64_app_files"))
    const runtime = require(bootstrap) as { TOTAL_BUDGET_MS: number }
    expect(runtime.TOTAL_BUDGET_MS).toBe(15 * 60 * 1000)
  })

  it("locks generated cleanup to the recorded archive output identity and refuses replacement trees", async () => {
    const outputParent = await temp("agora-generated-owner-")
    const first = await packaging.createOwnedGeneratedDirectory(outputParent)
    await writeFile(path.join(first.directory, "owned.txt"), "owned")
    await packaging.cleanupOwnedGeneratedDirectory(first)
    await expect(stat(first.directory)).rejects.toMatchObject({ code: "ENOENT" })

    const replaced = await packaging.createOwnedGeneratedDirectory(outputParent)
    await rm(replaced.directory, { recursive: true })
    await mkdir(replaced.directory)
    const foreign = path.join(replaced.directory, "foreign.txt"); await writeFile(foreign, "do-not-delete")
    await expect(packaging.cleanupOwnedGeneratedDirectory(replaced)).rejects.toThrow(/identity changed/)
    await expect(readFile(foreign, "utf8")).resolves.toBe("do-not-delete")
  })

  it("cleans an owned partial generated directory when input generation throws before registration", async () => {
    const fixture = await payloadFixture("partial-generation")
    const templateRoot = await packaging.verifyTemplates(desktopDir)
    const before = (await readdir(fixture.root)).filter(name => name.startsWith(".agora-nsis-"))
    await expect(packaging.writeGeneratedInputs({
      archive: fixture.archive,
      appOutDir: fixture.appOutDir,
      defines: { APP_64_HASH: fixture.archiveSha512.toUpperCase(), APP_ID: "com.agorahub.fixture", PRODUCT_FILENAME: "Fixture" },
      projectDir: path.join(fixture.root, "bad$project"),
      templateRoot,
      extractor: fixture.tool.extractor
    })).rejects.toThrow(/fixed NSIS literal/)
    const after = (await readdir(fixture.root)).filter(name => name.startsWith(".agora-nsis-"))
    expect(after).toEqual(before)
  })

  it("compares the complete archive inventory with the independently authored payload manifest", async () => {
    const fixture = await payloadFixture("archive-gate")
    const manifestBytes = await readFile(fixture.manifest)
    await expect(packaging.verifyArchiveInventory({ extractor: fixture.tool.extractor, archive: fixture.archive, manifestBytes })).resolves.toBeUndefined()
    await writeFile(path.join(fixture.appOutDir, "unexpected.txt"), "unexpected")
    const changedArchive = path.join(fixture.root, "changed.7z"); await archiveDirectory(fixture.tool.extractor, fixture.appOutDir, changedArchive)
    await expect(packaging.verifyArchiveInventory({ extractor: fixture.tool.extractor, archive: changedArchive, manifestBytes })).rejects.toThrow(/inventory count/)
  })

  it("rejects hard-linked payload files instead of blessing a non-independent tree", async () => {
    const fixture = await payloadFixture("hardlink")
    const original = path.join(fixture.appOutDir, "Agora.exe"), alias = path.join(fixture.appOutDir, "Agora-copy.exe")
    await link(native(original), native(alias))
    await expect(packaging.authorPayloadManifest({ appOutDir: fixture.appOutDir, archiveSha512: fixture.archiveSha512, productId: "com.agorahub.fixture", uninstallerFile: "Uninstall Fixture.exe", preFiles: [] })).rejects.toThrow(/hard link/)
  })
})

describe("embedded installer bootstrap", () => {
  it("directly extracts and fully verifies a >260 payload, then deletes the owned tree while preserving profile data", async () => {
    const fixture = await payloadFixture("long")
    const installRoot = path.join(fixture.root, "installed")
    const profile = path.join(fixture.root, "profile", "sentinel.json"); await mkdir(path.dirname(profile), { recursive: true }); await writeFile(profile, "profile-preserved")
    const self = await installAndSeal(fixture, installRoot)
    const longItem = fixture.resources.find(item => item.path.startsWith("licenses/"))!
    expect(path.join(installRoot, "resources", "runtime", ...longItem.path.split("/")).length).toBeGreaterThan(260)
    expect(await readFile(native(path.join(installRoot, "resources", "runtime", ...longItem.path.split("/"))), "utf8")).toBe("license-long")
    await runBootstrap(["uninstall", fixture.manifest, fixture.manifestSha512, installRoot, "com.agorahub.fixture", self, path.join(fixture.root, "uninstall.json")])
    await expect(stat(native(installRoot))).rejects.toMatchObject({ code: "ENOENT" })
    await expect(readFile(profile, "utf8")).resolves.toBe("profile-preserved")
  })

  it("supports owned old payload preflight -> removal -> new payload -> removal without claiming rollback", async () => {
    const oldFixture = await payloadFixture("old"), newFixture = await payloadFixture("new")
    const chainRoot = await temp("agora-owned-upgrade-")
    const installRoot = path.join(chainRoot, "installed"), profile = path.join(chainRoot, "profile", "sentinel")
    await mkdir(path.dirname(profile), { recursive: true }); await writeFile(profile, "keep")
    const oldSelf = await installAndSeal(oldFixture, installRoot)
    await expect(runBootstrap(["admit-owned", installRoot, "com.agorahub.fixture", path.join(chainRoot, "admit-old.json")])).resolves.toBeTruthy()
    await expect(runBootstrap(installArgs("preflight", newFixture, installRoot, path.join(chainRoot, "preflight-new.json")))).resolves.toBeTruthy()
    await runBootstrap(["uninstall", oldFixture.manifest, oldFixture.manifestSha512, installRoot, "com.agorahub.fixture", oldSelf, path.join(chainRoot, "remove-old.json")])
    const newSelf = await installAndSeal(newFixture, installRoot)
    await expect(readFile(path.join(installRoot, "Agora.exe"), "utf8")).resolves.toBe("electron-new")
    await runBootstrap(["uninstall", newFixture.manifest, newFixture.manifestSha512, installRoot, "com.agorahub.fixture", newSelf, path.join(chainRoot, "remove-new.json")])
    await expect(stat(installRoot)).rejects.toMatchObject({ code: "ENOENT" })
    await expect(readFile(profile, "utf8")).resolves.toBe("keep")
  })

  it("fails closed for poison environment, bad pins, manifest tails, archive extras and copy/root errors", async () => {
    const fixture = await payloadFixture("failures"), installRoot = path.join(fixture.root, "installed")
    const poisoned = cleanEnvironment({ AGORA_NSIS_EXTRACTOR: "poison" })
    await expect(runBootstrap(installArgs("preflight", fixture, installRoot, path.join(fixture.root, "poison.json")), poisoned)).rejects.toMatchObject({ code: 60 })
    const badPin = installArgs("preflight", fixture, installRoot, path.join(fixture.root, "bad-pin.json")); badPin[4] = "0".repeat(128)
    await expect(runBootstrap(badPin)).rejects.toMatchObject({ code: 61 })

    const tailed = path.join(fixture.root, "tailed.json"), tailedBytes = Buffer.concat([await readFile(fixture.manifest), Buffer.from("TAIL")]); await writeFile(tailed, tailedBytes)
    const tailArgs = installArgs("preflight", fixture, installRoot, path.join(fixture.root, "tail.json")); tailArgs[5] = tailed; tailArgs[6] = digest(tailedBytes, "sha512")
    await expect(runBootstrap(tailArgs)).rejects.toMatchObject({ code: 62 })

    const extra = path.join(fixture.appOutDir, "extra.txt"); await writeFile(extra, "extra")
    const extraArchive = path.join(fixture.root, "extra.7z"); await archiveDirectory(fixture.tool.extractor, fixture.appOutDir, extraArchive)
    const extraArgs = installArgs("preflight", fixture, installRoot, path.join(fixture.root, "extra.json")); extraArgs[3] = extraArchive; extraArgs[4] = await fileDigest(extraArchive, "sha512")
    const manifestObject = JSON.parse(await readFile(fixture.manifest, "utf8")); manifestObject.archiveSha512 = extraArgs[4]
    const rebound = Buffer.from(JSON.stringify(manifestObject) + "\n"), reboundPath = path.join(fixture.root, "rebound.json"); await writeFile(reboundPath, rebound); extraArgs[5] = reboundPath; extraArgs[6] = digest(rebound, "sha512")
    await expect(runBootstrap(extraArgs)).rejects.toMatchObject({ code: 61 })

    const parentFile = path.join(fixture.root, "not-a-directory"); await writeFile(parentFile, "x")
    await expect(runBootstrap(installArgs("install", fixture, path.join(parentFile, "child"), path.join(fixture.root, "copy-error.json")))).rejects.toMatchObject({ code: 60 })
  })

  it("revalidates root, ancestors and entries before each delete and never follows a replacement junction", async () => {
    const fixture = await payloadFixture("delete-race"), installRoot = path.join(fixture.root, "installed"), self = await installAndSeal(fixture, installRoot)
    const runtime = require(bootstrap) as {
      planOwnedInstallationDeletion(root: string, productId: string, selfPath: string, deadline: number): Promise<any>
      executeOwnedInstallationDeletion(plan: any, deadline: number): Promise<any>
    }
    const deadline = Date.now() + 60_000
    const plan = await runtime.planOwnedInstallationDeletion(installRoot, "com.agorahub.fixture", self, deadline)
    const licenses = path.join(installRoot, "resources", "runtime", "licenses")
    const quarantined = path.join(fixture.root, "verified-licenses-moved-after-plan")
    const external = path.join(fixture.root, "external-target"); await mkdir(external)
    const sentinel = path.join(external, "sentinel.txt"); await writeFile(sentinel, "outside-owned-tree")
    await rename(native(licenses), native(quarantined))
    await symlink(external, licenses, "junction")

    await expect(runtime.executeOwnedInstallationDeletion(plan, deadline)).rejects.toMatchObject({ code: 70, phase: "delete-identity" })
    await expect(readFile(sentinel, "utf8")).resolves.toBe("outside-owned-tree")
    await expect(stat(installRoot)).resolves.toBeTruthy()
    await expect(stat(quarantined)).resolves.toBeTruthy()
  })

  it("rejects unknown files at uninstall and enforces a true child deadline without an orphan", async () => {
    const fixture = await payloadFixture("unknown"), installRoot = path.join(fixture.root, "installed"), self = await installAndSeal(fixture, installRoot)
    await writeFile(path.join(installRoot, "unknown.txt"), "unknown")
    await expect(runBootstrap(["uninstall", fixture.manifest, fixture.manifestSha512, installRoot, "com.agorahub.fixture", self, path.join(fixture.root, "unknown-result.json")])).rejects.toMatchObject({ code: 62 })
    await expect(readFile(path.join(installRoot, "unknown.txt"), "utf8")).resolves.toBe("unknown")

    // Observe the OS child, not JavaScript startup: the real deadline may kill it
    // before it executes any fixture code. Keep this loader private to this test.
    const realChildProcess = require("node:child_process") as typeof import("node:child_process")
    const spawned: Array<{ pid: number | undefined; child: ChildProcess; closed: boolean }> = []
    const observedChildProcess = {
      ...realChildProcess,
      spawn(file: string, args: readonly string[], options: SpawnOptions) {
        const child = realChildProcess.spawn(file, args, options)
        const observed = { pid: child.pid, child, closed: false }
        spawned.push(observed)
        child.once("close", () => { observed.closed = true })
        return child
      }
    }
    const bootstrapRequire = createRequire(bootstrap)
    const observedRequire = Object.assign(
      (id: string) => id === "node:child_process" ? observedChildProcess : bootstrapRequire(id),
      bootstrapRequire
    )
    const runtimeModule = { exports: {} }
    const loadBootstrap = compileFunction(await readFile(bootstrap, "utf8"), ["exports", "require", "module", "__filename", "__dirname"], { filename: bootstrap })
    loadBootstrap(runtimeModule.exports, observedRequire, runtimeModule, bootstrap, path.dirname(bootstrap))
    const runtime = runtimeModule.exports as { runFile(file: string, args: string[], options: { deadline: number; limitMs: number }): Promise<unknown> }
    await expect(runtime.runFile(process.execPath, ["-e", "setInterval(()=>{},1000)"], { deadline: Date.now() + 5_000, limitMs: 150 })).rejects.toMatchObject({ code: 124 })
    expect(spawned).toHaveLength(1)
    const observed = spawned[0]
    expect(observed.pid).toBeTypeOf("number")
    expect(observed.pid).toBeGreaterThan(0)
    expect(observed.child.killed).toBe(true)
    expect(observed.closed).toBe(true)
    expect(() => process.kill(observed.pid!, 0)).toThrow()
  })
})


describe("bounded streaming NSIS archive inventory", () => {
  const runtime = require(bootstrap) as {
    createArchiveInventoryParser(parsed: any): { limits: { maxBytes: number; maxLineBytes: number; expectedCount: number }; push(chunk: Buffer): void; finish(): any }
    runFile(file: string, args: string[], options: Record<string, unknown>): Promise<any>
  }
  function expected(files: Array<[string, number]> = [["file.txt", 3]], directories: string[] = []) {
    return { files: new Map(files.map(([name, size]) => [name.toLowerCase(), { path: name, size }])), directories: new Map(directories.map(name => [name.toLowerCase(), name])) }
  }
  function record(name: string, size = 3, directory = false) {
    return `Path = ${name}\nSize = ${size}\nPacked Size = 0\nModified = \nAttributes = ${directory ? "D" : "A"}\nCRC = \nEncrypted = -\nMethod = LZMA2:20\nBlock = 0\n\n`
  }
  function parse(text: string | Buffer, inventory = expected()) {
    const parser = runtime.createArchiveInventoryParser(inventory)
    parser.push(Buffer.isBuffer(text) ? text : Buffer.from(text))
    return parser.finish()
  }
  it("matches exact files, directories and sizes across byte-split UTF-8/CRLF chunks without collecting stdout", () => {
    const inventory = expected([["目录/a = 🙂.txt", 3]], ["目录"])
    const bytes = Buffer.from((record("目录", 0, true) + record("目录\\a = 🙂.txt")).replaceAll("\n", "\r\n"))
    const parser = runtime.createArchiveInventoryParser(inventory)
    for (let at = 0; at < bytes.length; at++) parser.push(bytes.subarray(at, at + 1))
    expect(parser.finish()).toMatchObject({ listingBytes: bytes.length, members: 2, files: 1, directories: 1 })
    expect(() => parser.push(Buffer.alloc(0))).toThrow(/already closed/)
  })
  it.each([
    ["invalid continuation", Buffer.from([0xc3, 0x28])],
    ["truncated codepoint", Buffer.from([0xf0, 0x9f, 0x99])],
    ["overlong ASCII", Buffer.from([0xc0, 0xaf])]
  ])("fails closed for %s UTF-8 bytes", (_label, invalid) => {
    expect(() => parse(Buffer.concat([Buffer.from("Path = "), invalid, Buffer.from("\n")]))).toThrow(/not valid UTF-8/)
  })
  it("rejects truncated UTF-8 even without a final newline", () => {
    expect(() => parse(Buffer.concat([Buffer.from("Path = "), Buffer.from([0xe4, 0xb8])]))).toThrow(/not valid UTF-8/)
  })
  it.each(["../file.txt", "C:\\file.txt", "\\file.txt", "dir/../file.txt", "file.txt.", "dir/con.txt", "file.txt:stream"])("rejects unsafe Windows member path %s", member => {
    expect(() => parse(record(member))).toThrow(/unsafe path|absolute path/)
  })
  it("rejects repeated paths and Windows case collisions, including file/directory aliases", () => {
    const inventory = expected([["file.txt", 3], ["other.txt", 3]])
    for (const alias of [record("file.txt"), record("FILE.TXT"), record("file.txt", 0, true)]) {
      expect(() => parse(record("file.txt") + alias, inventory)).toThrow(/duplicate or Windows-colliding/)
    }
  })
  it("rejects duplicate fields rather than letting the last Path or type win", () => {
    for (const field of ["Path = file.txt", "Size = 3", "Attributes = A"]) {
      expect(() => parse(record("file.txt").replace("CRC = ", field + "\nCRC = "))).toThrow(/duplicate field/)
    }
  })
  it("requires the exact complete directory inventory, not just every file", () => {
    const inventory = expected([["dir/file.txt", 3]], ["dir"])
    expect(() => parse(record("dir/file.txt"), inventory)).toThrow(/inventory count/)
    expect(() => parse(record("DIR", 0, true) + record("dir/file.txt"), inventory)).toThrow(/inventory count\/path\/type/)
    expect(() => parse(record("dir", 0, true) + record("dir/file.txt") + record("extra", 0, true), inventory)).toThrow(/inventory count/)
  })
  it.each([
    ["missing file", ""],
    ["extra file", record("file.txt") + record("extra.txt")],
    ["case mismatch", record("FILE.TXT")],
    ["wrong type", record("file.txt", 0, true)],
    ["wrong size", record("file.txt", 4)],
    ["invalid size", record("file.txt").replace("Size = 3", "Size = -1")],
    ["encrypted member", record("file.txt").replace("Encrypted = -", "Encrypted = +")],
    ["incomplete record", "Path = file.txt\n\n"],
    ["conflicting Folder", record("file.txt").replace("CRC = ", "Folder = +\nCRC = ")],
    ["unknown field", record("file.txt").replace("CRC = ", "Hard Link = outside\nCRC = ")],
    ["unstructured output", "not a technical listing\n"],
    ["control byte", record("file.txt").replace("CRC = ", "CRC = \u0000")]
  ])("rejects %s", (_label, text) => {
    expect(() => parse(text)).toThrow()
  })
  it("derives total and per-line byte limits from the manifest and enforces them across chunks", () => {
    const parser = runtime.createArchiveInventoryParser(expected())
    const bigger = runtime.createArchiveInventoryParser(expected([["file.txt", 3], ["other.txt", 3]]))
    expect(bigger.limits.expectedCount).toBe(2)
    expect(bigger.limits.maxBytes).toBeGreaterThan(parser.limits.maxBytes)
    parser.push(Buffer.alloc(parser.limits.maxBytes, 10))
    expect(() => parser.push(Buffer.from("\n"))).toThrow(/total byte budget/)
    const line = runtime.createArchiveInventoryParser(expected())
    line.push(Buffer.alloc(line.limits.maxLineBytes, 65))
    expect(() => line.push(Buffer.from("AA"))).toThrow(/line exceeded/)
    expect(() => parse(record("file.txt").replace("Method = LZMA2:20", "Method = " + "a".repeat(950)), expected([["file.txt", 3], ["other.txt", 3]]))).toThrow(/record exceeded/)
  })
  it("streams a child listing through the production runner and returns only bounded inventory statistics", async () => {
    const text = record("file.txt")
    const result = await runtime.runFile(process.execPath, ["-e", `process.stdout.write(${JSON.stringify(text)})`], {
      deadline: Date.now() + 10_000, limitMs: 5_000, maxBuffer: 256,
      stdoutParser: runtime.createArchiveInventoryParser(expected())
    })
    expect(result.stdout).toBe("")
    expect(result.inventory).toMatchObject({ members: 1, files: 1, directories: 0, listingBytes: Buffer.byteLength(text) })
  })
  it("kills and reaps a child on parser failure before rejecting, without retaining further output", async () => {
    const root = await temp("agora-listing-child-")
    const pidFile = path.join(root, "pid.txt")
    const code = `require('fs').writeFileSync(${JSON.stringify(pidFile)}, String(process.pid)); process.stdout.write(Buffer.from([255,10])); setInterval(()=>process.stdout.write('more'),10)`
    await expect(runtime.runFile(process.execPath, ["-e", code], {
      deadline: Date.now() + 10_000, limitMs: 5_000, maxBuffer: 256,
      stdoutParser: runtime.createArchiveInventoryParser(expected())
    })).rejects.toThrow(/not valid UTF-8/)
    const pid = Number(await readFile(pidFile, "utf8"))
    expect(() => process.kill(pid, 0)).toThrow()
  })
})
