'use strict'

const { createHash } = require('node:crypto')
const { constants, createReadStream } = require('node:fs')
const fs = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { parsePayloadManifest, verifyArchiveListing } = require('../assets/nsis/installer-bootstrap.cjs')

const APP_BUILDER_VERSION = '26.15.3'
const APP_BUILDER_INTEGRITY = 'sha512-2VnyWkqsP5v5XbBhL3tD5Syx8iNPBYsoU7kY4S2fz7wg8Rj/nztWKCUzGKaFRTv0Xwf3/H058CR1Kvtd/3lRow=='
const EXTRACTOR_VERSION = '24.09'
const EXTRACTOR_SIZE = 849920
const EXTRACTOR_SHA256 = '223b873c50380fe9a39f1a22b6abf8d46db506e1c08d08312902f6f3cd1f7ac3'
const EXTRACTOR_LICENSES = Object.freeze([
  { name: 'COPYING', size: 27032, sha256: '1e7e6bae5a5bde32f1ae5a7c37a082d1ab03cf89354f7f936ac40be9e39a6531' },
  { name: 'LICENSE.txt', size: 6341, sha256: '7b7022cccdee7bddd07f5fd847161e33551cec411e9c9967c14008ee36d5ce3f' }
])
const TEMPLATE_LOCK = Object.freeze({
  'installer.nsi': { size: 3063, sha256: '8811964416d122612c3e7601728af5d3f998d677918df829a4e8b4c739c8b9f8' },
  'installSection.nsh': { size: 2705, sha256: '74c4dbb4163cdd90ca4850b75d304e916235c882d6af7b5cdad63bffbc977072' },
  'include/installer.nsh': { size: 8790, sha256: '0e319437dd01dcbf911f3f48f664fde0cefbaef704f1cdb1739f63d563f5d4a0' },
  'include/extractAppPackage.nsh': { size: 3189, sha256: 'e4174388a0f7a1df0b85a0742aa1ea7a4b2b18f9f29dccd6ef10a66212f68148' }
})
const HASH_WORKERS = 8
const RESERVED_INSTALL_FILES = new Set(['.agora-install-owner.json', '.agora-install-payload.json'])

function reject(message) { throw new Error(message) }
function resolved(value) { return path.resolve(value) }
function nativePath(value) { return process.platform === 'win32' ? path.toNamespacedPath(resolved(value)) : resolved(value) }
function safeRelative(value) {
  if (typeof value !== 'string' || !value || value.includes('\\') || value.includes('\0') || path.posix.isAbsolute(value) || path.posix.normalize(value) !== value) return false
  return !value.split('/').some(part => !part || part === '.' || part === '..' || /[<>:"|?*\x00-\x1f]/.test(part) || /[ .]$/.test(part) || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(part))
}
function assertNsisLiteral(value, label) { if (typeof value !== 'string' || /["$!\r\n]/.test(value)) reject(label + ' cannot be represented as a fixed NSIS literal'); return value }
async function sha(file, algorithm = 'sha256') { const hash = createHash(algorithm); for await (const chunk of createReadStream(nativePath(file))) hash.update(chunk); return hash.digest('hex') }
async function parallel(items, action) {
  let cursor = 0
  const results = await Promise.allSettled(Array.from({ length: Math.min(HASH_WORKERS, Math.max(1, items.length)) }, async () => { while (cursor < items.length) await action(items[cursor++]) }))
  const failed = results.find(result => result.status === 'rejected'); if (failed) throw failed.reason
}
async function plainDirectory(directory) {
  const target = resolved(directory), root = path.parse(target).root; let cursor = root
  for (const part of path.relative(root, target).split(path.sep).filter(Boolean)) { cursor = path.join(cursor, part); const stat = await fs.lstat(nativePath(cursor)); if (!stat.isDirectory() || stat.isSymbolicLink()) reject('build path contains a link or junction: ' + cursor) }
}
async function directoryIdentity(directory) {
  const target = resolved(directory)
  await plainDirectory(target)
  const stat = await fs.lstat(nativePath(target), { bigint: true })
  if (!stat.isDirectory() || stat.isSymbolicLink()) reject('generated NSIS owner must be a plain directory: ' + target)
  return { target, real: resolved(await fs.realpath(nativePath(target))), dev: String(stat.dev), ino: String(stat.ino) }
}
function samePath(left, right) { return resolved(left).toLowerCase() === resolved(right).toLowerCase() }
async function verifyOwnedGeneratedDirectory(token) {
  if (!token || token.prefix !== '.agora-nsis-' || !path.isAbsolute(token.archiveOutputParent) || !path.isAbsolute(token.directory)) reject('generated NSIS cleanup token is invalid')
  const parent = await directoryIdentity(token.archiveOutputParent)
  if (!samePath(parent.target, token.parent.target) || !samePath(parent.real, token.parent.real) || parent.dev !== token.parent.dev || parent.ino !== token.parent.ino) reject('generated NSIS output parent identity changed')
  const base = path.basename(token.directory)
  if (!samePath(path.dirname(token.directory), token.archiveOutputParent) || !base.startsWith(token.prefix) || base.length <= token.prefix.length) reject('generated NSIS directory escaped its locked output parent')
  await plainDirectory(token.directory)
  const stat = await fs.lstat(nativePath(token.directory), { bigint: true })
  if (!stat.isDirectory() || stat.isSymbolicLink() || String(stat.dev) !== token.dev || String(stat.ino) !== token.ino) reject('generated NSIS directory identity changed')
  const real = resolved(await fs.realpath(nativePath(token.directory)))
  if (!samePath(real, token.real) || !samePath(path.dirname(real), token.parent.real)) reject('generated NSIS directory was redirected')
}
async function createOwnedGeneratedDirectory(outputParent) {
  const archiveOutputParent = resolved(outputParent)
  const parent = await directoryIdentity(archiveOutputParent)
  const prefix = '.agora-nsis-'
  const directory = resolved(await fs.mkdtemp(path.join(parent.target, prefix)))
  const stat = await fs.lstat(nativePath(directory), { bigint: true })
  const token = { archiveOutputParent, parent, prefix, directory, real: resolved(await fs.realpath(nativePath(directory))), dev: String(stat.dev), ino: String(stat.ino) }
  await verifyOwnedGeneratedDirectory(token)
  return token
}
async function cleanupOwnedGeneratedDirectory(token) {
  await verifyOwnedGeneratedDirectory(token)
  await fs.rm(nativePath(token.directory), { recursive: true, force: false })
}
async function verifyFile(file, expected, label) {
  const stat = await fs.lstat(nativePath(file))
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size !== expected.size) reject(label + ' size/type/link identity mismatch')
  if (await sha(file) !== expected.sha256) reject(label + ' SHA-256 mismatch')
}
async function verifyBuilderLock(projectDir) {
  const packageJson = JSON.parse(await fs.readFile(path.join(projectDir, 'node_modules/app-builder-lib/package.json'), 'utf8'))
  if (packageJson.version !== APP_BUILDER_VERSION) reject('app-builder-lib version drift: expected ' + APP_BUILDER_VERSION)
  const lock = JSON.parse(await fs.readFile(path.join(projectDir, 'package-lock.json'), 'utf8'))
  const entry = lock.packages && lock.packages['node_modules/app-builder-lib']
  if (!entry || entry.version !== APP_BUILDER_VERSION || entry.integrity !== APP_BUILDER_INTEGRITY) reject('app-builder-lib lockfile identity drift')
}
async function verifyTemplates(projectDir) {
  await verifyBuilderLock(projectDir)
  const root = path.join(projectDir, 'node_modules/app-builder-lib/templates/nsis'); await plainDirectory(root)
  for (const [relative, pin] of Object.entries(TEMPLATE_LOCK)) await verifyFile(path.join(root, ...relative.split('/')), pin, 'app-builder-lib NSIS template ' + relative)
  return root
}
async function walkPlain(root, maxDepth, visit, depth = 0) {
  if (depth > maxDepth) return
  for (const entry of await fs.readdir(nativePath(root), { withFileTypes: true })) {
    const absolute = path.join(root, entry.name), stat = await fs.lstat(nativePath(absolute))
    if (stat.isSymbolicLink()) reject('extractor cache contains a link or junction: ' + absolute)
    if (stat.isDirectory()) await walkPlain(absolute, maxDepth, visit, depth + 1); else if (stat.isFile()) await visit(absolute, stat)
  }
}
async function resolvePinnedExtractor() {
  const cacheRoot = path.join(os.homedir(), 'AppData', 'Local', 'electron-builder', 'Cache', '7zip@1.0.0'); await plainDirectory(cacheRoot)
  const candidates = []
  await walkPlain(cacheRoot, 4, async (file, stat) => { if (path.basename(file).toLowerCase() === '7za.exe' && path.basename(path.dirname(file)).toLowerCase() === 'bin' && stat.size === EXTRACTOR_SIZE && await sha(file) === EXTRACTOR_SHA256) candidates.push(file) })
  if (candidates.length !== 1) reject('expected exactly one pinned modern 7za ' + EXTRACTOR_VERSION + ' in electron-builder cache; found ' + candidates.length)
  const extractor = candidates[0], distribution = path.dirname(path.dirname(extractor))
  for (const license of EXTRACTOR_LICENSES) await verifyFile(path.join(distribution, license.name), license, 'modern 7za license ' + license.name)
  return { extractor, distribution }
}
async function inventory(root) {
  const files = new Map(), directories = new Map()
  async function walk(directory, relativeDirectory) {
    const entries = await fs.readdir(nativePath(directory), { withFileTypes: true }); entries.sort((a, b) => a.name.localeCompare(b.name, 'en'))
    for (const entry of entries) {
      const absolute = path.join(directory, entry.name), relative = (relativeDirectory ? relativeDirectory + '/' : '') + entry.name
      if (!safeRelative(relative)) reject('unsafe payload path: ' + relative)
      const stat = await fs.lstat(nativePath(absolute)); if (stat.isSymbolicLink()) reject('payload contains a link or junction: ' + relative)
      const key = relative.toLowerCase(); if (files.has(key) || directories.has(key)) reject('payload contains duplicate or Windows-colliding paths: ' + relative)
      if (stat.isDirectory()) { directories.set(key, relative); await walk(absolute, relative) }
      else if (stat.isFile()) { if (stat.nlink !== 1) reject('payload contains a hard link: ' + relative); files.set(key, { path: relative, absolute, size: stat.size }) }
      else reject('payload contains an unsupported filesystem entry: ' + relative)
    }
  }
  await walk(root, ''); return { files, directories }
}
async function authorPayloadManifest({ appOutDir, archiveSha512, productId, uninstallerFile, preFiles }) {
  await plainDirectory(appOutDir)
  const tree = await inventory(appOutDir)
  for (const reserved of RESERVED_INSTALL_FILES) if (tree.files.has(reserved) || tree.directories.has(reserved)) reject('payload collides with reserved installer ownership file: ' + reserved)
  const entries = [...tree.files.values()]; await parallel(entries, async entry => { entry.sha256 = await sha(entry.absolute) })
  const runtimeManifestPath = path.join(appOutDir, 'resources', 'runtime', 'desktop-bundle.json'), runtimeManifestBytes = await fs.readFile(nativePath(runtimeManifestPath)), runtimeManifestSha256 = createHash('sha256').update(runtimeManifestBytes).digest('hex')
  let runtime; try { runtime = JSON.parse(runtimeManifestBytes.toString('utf8')) } catch { reject('packaged runtime manifest is invalid JSON') }
  if (runtime.schema !== 'newsroom.desktop-app-bundle/v1' || !Array.isArray(runtime.resources) || runtime.resources.length === 0) reject('packaged runtime manifest identity is invalid')
  const seenRuntime = new Set()
  for (const item of runtime.resources) {
    const payloadPath = 'resources/runtime/' + item.path, payload = tree.files.get(payloadPath.toLowerCase())
    if (!payload || payload.path !== payloadPath || payload.size !== item.size || payload.sha256 !== item.sha256) reject('runtime resource differs from complete payload inventory: ' + item.path)
    seenRuntime.add(payloadPath.toLowerCase())
  }
  const runtimePayload = [...tree.files.keys()].filter(key => key.startsWith('resources/runtime/') && key !== 'resources/runtime/desktop-bundle.json')
  if (runtimePayload.length !== seenRuntime.size || runtimePayload.some(key => !seenRuntime.has(key))) reject('packaged runtime contains stale or unmanifested payload files')
  const manifest = {
    schema: 'newsroom.desktop-installer-payload/v1', productId, archiveSha512, uninstallerFile,
    runtime: { root: 'resources/runtime', manifest: 'resources/runtime/desktop-bundle.json', manifestSha256: runtimeManifestSha256, resourceCount: runtime.resources.length },
    preFiles,
    directories: [...tree.directories.values()].sort((a, b) => a.localeCompare(b, 'en')),
    files: entries.map(({ path: filePath, size, sha256 }) => ({ path: filePath, size, sha256 })).sort((a, b) => a.path.localeCompare(b.path, 'en'))
  }
  return Buffer.from(JSON.stringify(manifest) + '\n')
}
async function copyExtractorLicenses(appOutDir, tool) {
  const target = path.join(appOutDir, 'resources', 'third-party', '7zip'); await fs.mkdir(nativePath(target), { recursive: true })
  for (const license of EXTRACTOR_LICENSES) { const destination = path.join(target, license.name); await fs.copyFile(nativePath(path.join(tool.distribution, license.name)), nativePath(destination), constants.COPYFILE_EXCL); await verifyFile(destination, license, 'packaged modern 7za license ' + license.name) }
}
function sha512HexFromBuilder(value) { if (typeof value !== 'string' || !/^[A-F0-9]{128}$/.test(value)) reject('electron-builder APP_64_HASH is missing or malformed'); return value.toLowerCase() }
async function verifyArchiveInventory({ extractor, archive, manifestBytes }) {
  const parsed = parsePayloadManifest(manifestBytes)
  await verifyArchiveListing(extractor, archive, parsed, Date.now() + 5 * 60 * 1000)
}
async function writeGeneratedInputs({ archive, appOutDir, defines, projectDir, templateRoot, extractor }) {
  const archiveStat = await fs.lstat(nativePath(archive))
  if (!archiveStat.isFile() || archiveStat.isSymbolicLink() || archiveStat.nlink !== 1) reject('electron-builder payload archive is not a plain independent file')
  const archiveSha512 = await sha(archive, 'sha512'); if (archiveSha512 !== sha512HexFromBuilder(defines.APP_64_HASH)) reject('electron-builder archive SHA-512 define differs from actual archive')
  const preFiles = []
  if (defines.UNINSTALLER_ICON) {
    const icon = resolved(defines.UNINSTALLER_ICON), stat = await fs.lstat(nativePath(icon))
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) reject('uninstaller icon is not a plain independent file')
    preFiles.push({ path: 'uninstallerIcon.ico', size: stat.size, sha256: await sha(icon) })
  }
  const uninstallerFile = 'Uninstall ' + defines.PRODUCT_FILENAME + '.exe'; if (!safeRelative(uninstallerFile) || uninstallerFile.includes('/')) reject('computed uninstaller filename is unsafe')
  const manifestBytes = await authorPayloadManifest({ appOutDir, archiveSha512, productId: defines.APP_ID, uninstallerFile, preFiles })
  await verifyArchiveInventory({ extractor, archive, manifestBytes })
  const cleanupToken = await createOwnedGeneratedDirectory(path.dirname(archive))
  const generatedDir = cleanupToken.directory, manifestPath = path.join(generatedDir, 'payload-manifest.json'), shimPath = path.join(generatedDir, 'installer.nsh')
  try {
    await fs.writeFile(nativePath(manifestPath), manifestBytes, { flag: 'wx' })
    const upstreamInstaller = path.join(templateRoot, 'include', 'installer.nsh'), bootstrap = path.join(projectDir, 'assets', 'nsis', 'installer-bootstrap.cjs'), overrides = path.join(projectDir, 'assets', 'nsis', 'installer-overrides.nsh'), functions = path.join(projectDir, 'assets', 'nsis', 'installer-functions.nsh')
    for (const [label, value] of Object.entries({ generatedDir, manifestPath, shimPath, upstreamInstaller, bootstrap, overrides, functions })) assertNsisLiteral(value, label)
    const shim = '!include "${AGORA_NSIS_UPSTREAM_INSTALLER_NSH}"\n!include "${AGORA_NSIS_INSTALLER_OVERRIDES_NSH}"\nCall AgoraPreflightOnce\n'
    await fs.writeFile(nativePath(shimPath), shim, { flag: 'wx' })
    return { cleanupToken, generatedDir, manifestPath, shimPath, upstreamInstaller, bootstrap, overrides, functions, manifestSha512: createHash('sha512').update(manifestBytes).digest('hex'), bootstrapSha512: await sha(bootstrap, 'sha512'), archiveSha512 }
  } catch (error) {
    try { await cleanupOwnedGeneratedDirectory(cleanupToken) }
    catch (cleanupError) { throw new AggregateError([error, cleanupError], 'generated NSIS inputs failed and owned partial cleanup was refused') }
    throw error
  }
}
function setFixedDefine(defines, name, value) { if (Object.prototype.hasOwnProperty.call(defines, name)) reject('mutable NSIS define override is forbidden: ' + name); defines[name] = assertNsisLiteral(String(value), name) }
function createNsisInstallerPackaging({ projectDir, runtimeHooks }) {
  if (!runtimeHooks || typeof runtimeHooks.getBuildRecords !== 'function') reject('installer packaging requires verified runtime build records')
  const generated = new Map(); let ready
  async function prerequisites() { if (!ready) ready = Promise.all([verifyTemplates(projectDir), resolvePinnedExtractor()]).then(([templateRoot, tool]) => ({ templateRoot, tool })); return ready }
  const hooks = {
    async beforePack(context) {
      if (context.electronPlatformName !== 'win32') reject('hardened NSIS packaging requires win32')
      const options = context.packager.packagerOptions
      if (options.effectiveOptionComputed && options.effectiveOptionComputed !== hooks.effectiveOptionComputed) reject('another effectiveOptionComputed hook is already installed')
      options.effectiveOptionComputed = hooks.effectiveOptionComputed
      await prerequisites()
    },
    async afterPack(context) { if (context.electronPlatformName !== 'win32') reject('hardened NSIS packaging requires win32'); const { tool } = await prerequisites(); await copyExtractorLicenses(context.appOutDir, tool) },
    async effectiveOptionComputed(options) {
      if (!Array.isArray(options) || options.length !== 2) reject('unexpected electron-builder effective options shape')
      const [defines] = options
      if (!defines.APP_64 || defines.APP_ARM64 || defines.APP_32) reject('hardened installer currently requires one x64 APP_64 payload')
      if (defines.COMPRESSION_METHOD !== '7z') reject('hardened installer requires electron-builder 7z payload compression')
      const records = runtimeHooks.getBuildRecords().filter(record => record.copied); if (records.length !== 1) reject('hardened installer requires exactly one verified packaged runtime owner')
      const record = records[0], { templateRoot, tool } = await prerequisites(), nodeSource = path.join(record.owner.root, 'node', 'node.exe'), archive = resolved(defines.APP_64)
      let inputs
      try {
        inputs = await writeGeneratedInputs({ archive, appOutDir: record.appOutDir, defines, projectDir, templateRoot, extractor: tool.extractor })
        const fixed = {
        AGORA_NSIS_TEMPLATES_ROOT: templateRoot, AGORA_NSIS_GENERATED_DIR: inputs.generatedDir, AGORA_NSIS_UPSTREAM_INSTALLER_NSH: inputs.upstreamInstaller, AGORA_NSIS_INSTALLER_OVERRIDES_NSH: inputs.overrides, AGORA_NSIS_INSTALLER_FUNCTIONS_NSH: inputs.functions,
        AGORA_NSIS_NODE_SOURCE: nodeSource, AGORA_NSIS_NODE_SHA512: await sha(nodeSource, 'sha512'), AGORA_NSIS_7ZA_SOURCE: tool.extractor,
        AGORA_NSIS_7ZA_SHA256: EXTRACTOR_SHA256, AGORA_NSIS_7ZA_SHA512: await sha(tool.extractor, 'sha512'), AGORA_NSIS_BOOTSTRAP_SOURCE: inputs.bootstrap,
        AGORA_NSIS_BOOTSTRAP_SHA512: inputs.bootstrapSha512, AGORA_NSIS_PAYLOAD_MANIFEST_SOURCE: inputs.manifestPath, AGORA_NSIS_PAYLOAD_MANIFEST_SHA512: inputs.manifestSha512,
        AGORA_NSIS_ARCHIVE_SHA512: inputs.archiveSha512, AGORA_NSIS_PRODUCT_ID: defines.APP_ID, AGORA_NSIS_UNINSTALLER_FILENAME: 'Uninstall ' + defines.PRODUCT_FILENAME + '.exe'
      }
        for (const [name, value] of Object.entries(fixed)) setFixedDefine(defines, name, value)
        generated.set(inputs.generatedDir, inputs.cleanupToken)
        return false
      } catch (error) {
        if (inputs) {
          try { await cleanupOwnedGeneratedDirectory(inputs.cleanupToken) }
          catch (cleanupError) { throw new AggregateError([error, cleanupError], 'effective NSIS options failed and generated input cleanup was refused') }
        }
        throw error
      }
    },
    async afterAllArtifactBuild() {
      const failures = []
      for (const [directory, token] of generated) {
        try { await cleanupOwnedGeneratedDirectory(token); generated.delete(directory) }
        catch (error) { failures.push(error) }
      }
      if (failures.length) throw new AggregateError(failures, 'one or more generated NSIS directories failed identity-checked cleanup')
      return []
    }
  }
  return hooks
}

module.exports = { APP_BUILDER_VERSION, EXTRACTOR_SHA256, TEMPLATE_LOCK, authorPayloadManifest, cleanupOwnedGeneratedDirectory, createNsisInstallerPackaging, createOwnedGeneratedDirectory, resolvePinnedExtractor, verifyArchiveInventory, verifyTemplates, writeGeneratedInputs }
