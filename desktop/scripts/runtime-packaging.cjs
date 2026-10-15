const { createHash } = require("node:crypto")
const { constants, createReadStream } = require("node:fs")
const fs = require("node:fs/promises")
const path = require("node:path")

const MANIFEST = "desktop-bundle.json"
const HASH_WORKERS = 8

// Keep lexical paths for ownership checks; only filesystem calls receive Windows
// extended paths. Glob walkers are not an inventory (e.g. .gitkeep is discarded).
function resolvedPath(value) {
  const resolved = path.resolve(value)
  if (process.platform !== "win32") return resolved
  if (resolved.startsWith("\\\\?\\UNC\\")) return "\\\\" + resolved.slice(8)
  if (/^\\\\\?\\[a-z]:\\/i.test(resolved)) return resolved.slice(4)
  if (resolved.startsWith("\\\\?\\") || resolved.startsWith("\\\\.\\")) reject("device namespace paths are not runtime ownership roots")
  return resolved
}
function nativePath(value) {
  const resolved = resolvedPath(value)
  // Node's Windows lstat/realpath reject an extended drive root itself.
  return resolved === path.parse(resolved).root ? resolved : path.toNamespacedPath(resolved)
}
function inside(root, candidate) {
  const relative = path.relative(root, candidate)
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative))
}
function reject(message) {
  throw new Error(message)
}
async function plainDirectory(directory, allowMissing = false) {
  const resolved = resolvedPath(directory)
  let current = path.parse(resolved).root
  for (const part of path.relative(current, resolved).split(path.sep).filter(Boolean)) {
    current = path.join(current, part)
    let info
    try { info = await fs.lstat(nativePath(current)) }
    catch (error) {
      if (allowMissing && error.code === "ENOENT") return resolved
      throw error
    }
    if (!info.isDirectory() || info.isSymbolicLink()) reject(`runtime path must contain only plain directories (no link or junction): ${current}`)
  }
  return resolved
}
function disjoint(source, destination) {
  if (inside(source, destination) || inside(destination, source)) reject("runtime source and build destination must be disjoint")
}
function safeRelative(value) {
  if (typeof value !== "string" || !value || value.includes("\\") || path.isAbsolute(value)) return false
  return !value.split("/").some(part => !part || part === "." || part === ".." || /[<>:"|?*\x00-\x1f]/.test(part) || /[ .]$/.test(part) || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(part))
}
function parseManifest(bytes) {
  let manifest
  try { manifest = JSON.parse(bytes.toString("utf8")) }
  catch { reject("missing or invalid verified bundle manifest") }
  if (manifest.schema !== "newsroom.desktop-app-bundle/v1" || typeof manifest.bundle_id !== "string" || !manifest.bundle_id || !Array.isArray(manifest.resources) || manifest.resources.length === 0) reject("desktop-bundle identity or resource inventory is invalid")
  if (manifest.runtime?.python?.path !== ".venv/Scripts/python.exe" || manifest.runtime?.node?.path !== "node/node.exe" || manifest.runtime?.node?.npm?.path !== "node/npm.cmd") reject("portable Python/Node/npm executable layout is invalid")
  const expected = new Set()
  const windowsNames = new Set([MANIFEST.toLowerCase()])
  for (const item of manifest.resources) {
    if (!item || !safeRelative(item.path) || !Number.isSafeInteger(item.size) || item.size < 0 || typeof item.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(item.sha256)) reject("resource inventory entry is invalid")
    const key = item.path.toLowerCase()
    if (windowsNames.has(key)) reject(`duplicate, Windows-colliding or reserved resource path: ${item.path}`)
    expected.add(item.path)
    windowsNames.add(key)
  }
  for (const relative of [manifest.runtime.python.path, manifest.runtime.node.path, manifest.runtime.node.npm.path]) {
    if (!expected.has(relative)) reject(`runtime executable is not included in the verified inventory: ${relative}`)
  }
  if (!expected.has("scripts/desktop_app.py") && !expected.has("scripts/desktop_app/__main__.py")) reject("scripts.desktop_app entry module is missing from the verified inventory")
  if (![...expected].some(item => item.startsWith("backend/"))) reject("backend source is missing from the verified runtime envelope")
  if (![...expected].some(item => item.startsWith("frontend/"))) reject("frontend build is missing from the verified runtime envelope")
  return manifest
}
async function parallel(items, action) {
  let index = 0
  // Wait for all workers even on failure: no background I/O may outlive a gate.
  const results = await Promise.allSettled(Array.from({ length: Math.min(HASH_WORKERS, items.length) }, async () => {
    while (index < items.length) await action(items[index++])
  }))
  const failed = results.find(result => result.status === "rejected")
  if (failed) throw failed.reason
}
async function sha256(file) {
  const hash = createHash("sha256")
  for await (const chunk of createReadStream(nativePath(file))) hash.update(chunk)
  return hash.digest("hex")
}
async function inventory(root, independentFiles) {
  const files = new Map()
  async function walk(directory) {
    for (const entry of await fs.readdir(nativePath(directory), { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name)
      const relative = path.relative(root, absolute).split(path.sep).join("/")
      const info = await fs.lstat(nativePath(absolute))
      if (info.isSymbolicLink()) reject(`runtime envelope contains a link or junction: ${relative}`)
      if (info.isDirectory()) await walk(absolute)
      else if (info.isFile()) {
        if (independentFiles && info.nlink !== 1) reject(`packaged runtime must not contain hard links: ${relative}`)
        files.set(relative, info)
      } else reject(`runtime envelope contains an unsupported filesystem entry: ${relative}`)
    }
  }
  await walk(root)
  return files
}

/** Validate the complete envelope, optionally against a frozen source owner. */
async function validateRuntimeStage(stage, { owner, independentFiles = false } = {}) {
  const root = await plainDirectory(stage)
  const actual = await inventory(root, independentFiles)
  if (!actual.has(MANIFEST)) reject(`missing or invalid verified bundle manifest at ${root}`)
  const bytes = await fs.readFile(nativePath(path.join(root, MANIFEST)))
  if (owner && !bytes.equals(owner.manifestBytes)) reject("bundle manifest differs from the verified source owner; manifest re-authoring is not permitted during packaging")
  const manifest = parseManifest(bytes)
  const expected = new Set(manifest.resources.map(item => item.path))
  for (const item of manifest.resources) {
    const info = actual.get(item.path)
    if (!info) reject(`resource is missing: ${item.path}`)
    if (info.size !== item.size) reject(`resource checksum mismatch: ${item.path}`)
  }
  for (const relative of actual.keys()) {
    if (relative !== MANIFEST && !expected.has(relative)) reject(`runtime envelope contains an unmanifested file: ${relative}`)
  }
  await parallel(manifest.resources, async item => {
    if (await sha256(path.join(root, ...item.path.split("/"))) !== item.sha256) reject(`resource checksum mismatch: ${item.path}`)
  })
  return { root, manifest, manifestBytes: bytes, manifestSha256: createHash("sha256").update(bytes).digest("hex"), resourceCount: manifest.resources.length }
}

// Resolve existing ancestors too, so short-name/namespace aliases cannot bypass
// source/destination separation before any directory or file is written.
async function canonicalDirectory(directory) {
  let current = await plainDirectory(directory, true)
  const missing = []
  for (;;) {
    try { return path.join(resolvedPath(await fs.realpath(nativePath(current))), ...missing.reverse()) }
    catch (error) {
      if (error.code !== "ENOENT") throw error
      const parent = path.dirname(current)
      if (parent === current) throw error
      missing.push(path.basename(current))
      current = parent
    }
  }
}

async function assertBuildDestination(source, destination) {
  const target = resolvedPath(destination)
  if (target === path.parse(target).root) reject("build destination must not be a filesystem root")
  disjoint(resolvedPath(source), target)
  disjoint(await canonicalDirectory(source), await canonicalDirectory(target))
  return target
}
async function ensureOwnedDirectory(ownedRoot, directory) {
  if (!inside(ownedRoot, directory)) reject("runtime destination escapes the owned build root")
  await plainDirectory(ownedRoot)
  let current = ownedRoot
  for (const part of path.relative(ownedRoot, directory).split(path.sep).filter(Boolean)) {
    current = path.join(current, part)
    try { await fs.mkdir(nativePath(current)) }
    catch (error) { if (error.code !== "EEXIST") throw error }
    const info = await fs.lstat(nativePath(current))
    if (!info.isDirectory() || info.isSymbolicLink()) reject(`runtime destination contains a link or junction: ${current}`)
  }
}

/** Fresh, exclusive, native file copies; never glob-copy, repair or hard-link. */
async function copyRuntimeStage(source, destination, { ownedRoot, owner } = {}) {
  if (!ownedRoot) reject("runtime copy requires an explicit owned build root")
  const root = resolvedPath(ownedRoot)
  const target = resolvedPath(destination)
  await assertBuildDestination(source, root)
  await plainDirectory(root)
  if (target === root || !inside(root, target)) reject("runtime destination must be strictly inside the owned build root")
  const verified = await validateRuntimeStage(source, { owner })
  await ensureOwnedDirectory(root, path.dirname(target))
  // Refuse an existing destination, even an empty one: never patch an envelope.
  await fs.mkdir(nativePath(target))
  const directories = new Set(verified.manifest.resources.map(item => path.dirname(path.join(target, ...item.path.split("/")))))
  for (const directory of directories) await ensureOwnedDirectory(target, directory)
  await parallel(verified.manifest.resources, async item => {
    const from = path.join(verified.root, ...item.path.split("/"))
    const to = path.join(target, ...item.path.split("/"))
    await fs.copyFile(nativePath(from), nativePath(to), constants.COPYFILE_EXCL)
  })
  await fs.copyFile(nativePath(path.join(verified.root, MANIFEST)), nativePath(path.join(target, MANIFEST)), constants.COPYFILE_EXCL)
  await validateRuntimeStage(verified.root, { owner: verified })
  return validateRuntimeStage(target, { owner: verified, independentFiles: true })
}

/** Builder lifecycle: copy before signing; gate after signing AND unconditionally
 * after all artifacts (--dir and builds without certificates included).
 * Runtime is deliberately absent from extraResources: its PE files must not
 * enter the Windows extra-file signing transformer. Sign the source runtime
 * before its manifest is authored if an owner-approved signed runtime is needed.
 */
function createRuntimePackagingHooks(source) {
  const builds = new Map()
  function record(context) {
    const item = builds.get(resolvedPath(context.appOutDir))
    if (!item) reject("packaged runtime has no verified source owner")
    return item
  }
  async function gate(item, phase) {
    try {
      const verified = await validateRuntimeStage(item.destination, { owner: item.owner, independentFiles: true })
      console.log(`Validated packaged runtime (${phase}): ${verified.resourceCount} resources; manifest sha256=${verified.manifestSha256}`)
    } catch (error) {
      throw new Error(`Packaged runtime validation failed (${phase}): ${error.message}. Runtime alteration, including code signing, is forbidden; signed runtime bytes must be owned by the source manifest before packaging.`, { cause: error })
    }
  }
  const hooks = {
    async beforePack(context) {
      if (context.electronPlatformName !== "win32") reject("verified runtime packaging currently requires win32")
      await assertBuildDestination(source, context.outDir)
      const appOutDir = await assertBuildDestination(source, context.appOutDir)
      const owner = await validateRuntimeStage(source)
      const resources = context.packager.getResourcesDir(appOutDir)
      builds.set(appOutDir, { owner, appOutDir, destination: path.join(resources, "runtime"), copied: false })
    },
    async afterPack(context) {
      const item = record(context)
      await copyRuntimeStage(source, item.destination, { ownedRoot: item.appOutDir, owner: item.owner })
      item.copied = true
      await gate(item, "afterPack")
    },
    async afterSign(context) {
      const item = record(context)
      if (!item.copied) reject("packaged runtime was not copied from its verified owner")
      await gate(item, "afterSign")
    },
    async afterAllArtifactBuild() {
      if (!builds.size) reject("build completed without a manifest-owned runtime copy; prepackaged bypass is forbidden")
      for (const item of builds.values()) {
        if (!item.copied) reject("build completed without a manifest-owned runtime copy")
        await validateRuntimeStage(source, { owner: item.owner })
        await gate(item, "afterAllArtifactBuild")
      }
      builds.clear()
      return []
    }
  }
  // Installer packaging needs the already-verified, post-sign app directory to
  // author its independent payload manifest. Keep this build-only capability
  // non-enumerable so spreading hooks into electron-builder config cannot turn
  // it into an unsupported lifecycle hook.
  Object.defineProperty(hooks, "getBuildRecords", {
    value: () => Array.from(builds.values()),
    enumerable: false
  })
  return hooks
}

module.exports = { nativePath, validateRuntimeStage, copyRuntimeStage, createRuntimePackagingHooks }

