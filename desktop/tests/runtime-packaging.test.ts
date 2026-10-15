import { createHash } from "node:crypto"
import { execFile } from "node:child_process"
import { promisify } from "node:util"
import { link, lstat, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { afterEach, describe, expect, it } from "vitest"

const { copyRuntimeStage, createRuntimePackagingHooks, validateRuntimeStage } = require("../scripts/runtime-packaging.cjs")
const exec = promisify(execFile)
const roots: string[] = []
const native = (value: string) => path.toNamespacedPath(path.resolve(value))
const digest = (bytes: Buffer) => createHash("sha256").update(bytes).digest("hex")

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), "agora-runtime-packaging-")); roots.push(root)
  const stage = path.join(root, "stage"), output = path.join(root, "build", "win-unpacked")
  const destination = path.join(output, "resources", "runtime")
  await mkdir(native(stage), { recursive: true }); await mkdir(native(output), { recursive: true })
  const longPath = `frontend/licenses/${"thirdparty-long-directory/".repeat(12)}LICENSE.txt`
  const files = new Map<string, string>([
    [".venv/Scripts/python.exe", "python"], ["node/node.exe", "node"], ["node/npm.cmd", "npm"],
    ["scripts/desktop_app.py", "entry"], ["backend/real.py", "backend"], ["frontend/index.html", "frontend"],
    ["frontend/src/.gitkeep", ""], ["frontend/.DS_Store", "hidden"],
    ["backend/__pycache__/real.cpython-312.pyc", "pyc"], [longPath, "long license"],
  ])
  const resources = []
  for (const [relative, content] of files) {
    const file = path.join(stage, ...relative.split("/")), bytes = Buffer.from(content)
    await mkdir(native(path.dirname(file)), { recursive: true }); await writeFile(native(file), bytes)
    resources.push({ path: relative, size: bytes.length, sha256: digest(bytes) })
  }
  const manifest = { schema: "newsroom.desktop-app-bundle/v1", bundle_id: "packaging-test",
    runtime: { python: { path: ".venv/Scripts/python.exe" }, node: { path: "node/node.exe", npm: { path: "node/npm.cmd" } } }, resources }
  await writeFile(native(path.join(stage, "desktop-bundle.json")), JSON.stringify(manifest, null, 2) + "\n")
  const context = { appOutDir: output, outDir: path.dirname(output), electronPlatformName: "win32",
    packager: { getResourcesDir: (dir: string) => path.join(dir, "resources") } }
  return { root, stage, output, destination, longPath, files, manifest, context }
}

afterEach(async () => Promise.all(roots.splice(0).map(root => {
  if (path.dirname(path.resolve(root)) !== path.resolve(os.tmpdir())) throw new Error("unsafe cleanup")
  return rm(native(root), { recursive: true, force: true })
})))

describe("runtime packaging", () => {
  it("copies the exact manifest, hidden files, pyc and long licenses without aliases or stage mutation", async () => {
    const { stage, output, destination, longPath, files } = await fixture()
    const owner = await validateRuntimeStage(stage)
    expect(path.join(destination, ...longPath.split("/")).length).toBeGreaterThan(260)
    const result = await copyRuntimeStage(stage, destination, { ownedRoot: output, owner })
    expect(result.manifestBytes).toEqual(owner.manifestBytes); expect(result.resourceCount).toBe(files.size)
    for (const [relative, content] of files) {
      const source = path.join(stage, ...relative.split("/")), target = path.join(destination, ...relative.split("/"))
      expect(await readFile(native(target))).toEqual(Buffer.from(content))
      expect((await lstat(native(target))).nlink).toBe(1); expect((await lstat(native(target))).ino).not.toBe((await lstat(native(source))).ino)
    }
    await writeFile(native(path.join(destination, "node/node.exe")), "mutated package")
    expect(await readFile(native(path.join(stage, "node/node.exe")))).toEqual(Buffer.from("node"))
  })

  it.each(["missing", "modified", "extra"])("fails closed for %s bytes", async kind => {
    const { stage, output, destination } = await fixture(); const owner = await validateRuntimeStage(stage)
    await copyRuntimeStage(stage, destination, { ownedRoot: output, owner })
    const file = path.join(destination, "backend/real.py")
    if (kind === "missing") await rm(native(file)); if (kind === "modified") await writeFile(native(file), "BACKEND")
    if (kind === "extra") await writeFile(native(path.join(destination, "frontend/extra.txt")), "extra")
    await expect(validateRuntimeStage(destination, { owner })).rejects.toThrow(kind === "extra" ? "unmanifested" : kind === "missing" ? "missing" : "checksum")
  })

  it("rejects reparse points, hard links, unsafe roots and existing destinations", async () => {
    const { root, stage, output, destination } = await fixture(); const foreign = path.join(root, "foreign")
    await mkdir(native(foreign)); await writeFile(native(path.join(foreign, "sentinel")), "safe")
    await symlink(native(foreign), native(path.join(stage, "backend/reparse")), "junction")
    await expect(validateRuntimeStage(stage)).rejects.toThrow(/link or junction/); await rm(native(path.join(stage, "backend/reparse")))
    const linkedStage = path.join(root, "linked-stage")
    await symlink(native(stage), native(linkedStage), "junction")
    await expect(copyRuntimeStage(linkedStage, destination, { ownedRoot: output })).rejects.toThrow(/link or junction/)
    await symlink(native(foreign), native(path.join(output, "resources")), "junction")
    await expect(copyRuntimeStage(stage, destination, { ownedRoot: output })).rejects.toThrow(/link or junction/)
    await rm(native(path.join(output, "resources")))
    await expect(copyRuntimeStage(stage, path.join(root, "outside"), { ownedRoot: output })).rejects.toThrow("strictly inside")
    await expect(copyRuntimeStage(stage, path.join(stage, "nested"), { ownedRoot: stage })).rejects.toThrow("disjoint")
    await expect(copyRuntimeStage(native(stage), path.join(stage, "namespace-alias"), { ownedRoot: stage })).rejects.toThrow("disjoint")
    await expect(copyRuntimeStage(stage, destination, { ownedRoot: path.parse(stage).root })).rejects.toThrow("filesystem root")
    const owner = await validateRuntimeStage(stage); await copyRuntimeStage(stage, destination, { ownedRoot: output, owner })
    const packaged = path.join(destination, "backend/real.py"); await rm(native(packaged)); await link(native(path.join(stage, "backend/real.py")), native(packaged))
    await expect(validateRuntimeStage(destination, { owner, independentFiles: true })).rejects.toThrow("hard links")
    await expect(copyRuntimeStage(stage, destination, { ownedRoot: output })).rejects.toThrow(/EEXIST/)
    expect(await readFile(native(path.join(foreign, "sentinel")))).toEqual(Buffer.from("safe"))
  })

  it("rejects manifest re-authoring and unsafe Windows paths", async () => {
    const { stage, output, destination, manifest } = await fixture(); const owner = await validateRuntimeStage(stage)
    await copyRuntimeStage(stage, destination, { ownedRoot: output, owner })
    const bytes = Buffer.from("signed python"); await writeFile(native(path.join(destination, ".venv/Scripts/python.exe")), bytes)
    Object.assign(manifest.resources[0], { size: bytes.length, sha256: digest(bytes) })
    await writeFile(native(path.join(destination, "desktop-bundle.json")), JSON.stringify(manifest))
    await expect(validateRuntimeStage(destination)).resolves.toBeTruthy()
    await expect(validateRuntimeStage(destination, { owner })).rejects.toThrow("source owner")
    manifest.resources[0].path = "frontend/CON.txt"; await writeFile(native(path.join(stage, "desktop-bundle.json")), JSON.stringify(manifest))
    await expect(validateRuntimeStage(stage)).rejects.toThrow("inventory entry")
  })

  it("gates no-certificate builds finally and blocks signer/runtime alteration", async () => {
    const { stage, context, destination } = await fixture(); const hooks = createRuntimePackagingHooks(stage)
    await hooks.beforePack(context); await hooks.afterPack(context)
    await expect(hooks.afterAllArtifactBuild()).resolves.toEqual([]) // no afterSign in unsigned build
    expect(await readFile(native(path.join(destination, "frontend/src/.gitkeep")))).toEqual(Buffer.alloc(0))
    const next = await fixture(); const signedHooks = createRuntimePackagingHooks(next.stage)
    await signedHooks.beforePack(next.context); await signedHooks.afterPack(next.context)
    await writeFile(native(path.join(next.destination, "node/node.exe")), "NODE")
    await expect(signedHooks.afterSign(next.context)).rejects.toThrow(/Runtime alteration.*code signing/)
    expect(await readFile(native(path.join(next.stage, "node/node.exe")))).toEqual(Buffer.from("node"))
  })

  it("freezes the source owner and fails if stage bytes change during the build", async () => {
    const { stage, context } = await fixture(); const hooks = createRuntimePackagingHooks(stage)
    await hooks.beforePack(context)
    await writeFile(native(path.join(stage, "node/node.exe")), "NODE")
    await expect(hooks.afterPack(context)).rejects.toThrow("checksum")
    await writeFile(native(path.join(stage, "node/node.exe")), "node")
    await hooks.afterPack(context)
    await hooks.afterSign(context)
    await writeFile(native(path.join(context.appOutDir, "resources/runtime/node/node.exe")), "NODE")
    await expect(hooks.afterAllArtifactBuild()).rejects.toThrow(/Runtime alteration.*code signing/)
  })

  it("uses matched config hooks and the same CLI validator", async () => {
    const config = require("../electron-builder.config.cjs")
    expect(config.extraResources).toEqual([]); expect(config).not.toHaveProperty("electronDist")
    for (const name of ["beforePack", "afterPack", "afterSign", "afterAllArtifactBuild"]) expect(config[name]).toBeTypeOf("function")
    const { stage } = await fixture(), script = path.resolve(__dirname, "../scripts/validate-runtime-stage.mjs")
    const valid = await exec(process.execPath, [script], { env: { ...process.env, AGORA_DESKTOP_RUNTIME_STAGE: stage } })
    expect(valid.stdout).toContain("10 resources")
    await rm(native(path.join(stage, "frontend/src/.gitkeep")))
    await expect(exec(process.execPath, [script], { env: { ...process.env, AGORA_DESKTOP_RUNTIME_STAGE: stage } })).rejects.toMatchObject({ code: 2 })
  })
})


