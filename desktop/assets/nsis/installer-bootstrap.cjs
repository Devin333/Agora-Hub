'use strict'

const { createHash } = require('node:crypto')
const { createReadStream } = require('node:fs')
const fs = require('node:fs/promises')
const path = require('node:path')
const { spawn } = require('node:child_process')

const OWNER_FILE = '.agora-install-owner.json'
const INSTALLED_MANIFEST = '.agora-install-payload.json'
const HASH_WORKERS = 8
const TOTAL_BUDGET_MS = 15 * 60 * 1000
const EXTRACT_BUDGET_MS = 5 * 60 * 1000
const BLOCKED_ENV = new Set([
  'AGORA_NSIS_EXTRACTOR', 'COMSPEC', 'NODE_OPTIONS', 'NODE_PATH',
  'NODE_REPL_EXTERNAL_MODULE', 'NODE_EXTRA_CA_CERTS', 'NODE_ICU_DATA',
  'NODE_V8_COVERAGE', 'OPENSSL_CONF', 'OPENSSL_MODULES', 'SSL_CERT_DIR',
  'SSL_CERT_FILE', 'PATH', 'PATHEXT'
])
const RESERVED_FILES = new Set([OWNER_FILE.toLowerCase(), INSTALLED_MANIFEST.toLowerCase()])
const activeChildren = new Set()

class InstallerError extends Error {
  constructor(code, phase, message, details = {}) {
    super(message)
    this.code = code
    this.phase = phase
    this.details = details
  }
}

function fail(code, phase, message, details) {
  throw new InstallerError(code, phase, message, details)
}
function nativePath(value) {
  const absolute = path.resolve(value)
  if (process.platform !== 'win32' || absolute === path.parse(absolute).root) return absolute
  return path.toNamespacedPath(absolute)
}
function checkDeadline(deadline, phase) {
  if (Date.now() > deadline) fail(124, 'deadline', 'installer bootstrap total deadline exceeded', { phase })
}
function canonicalAbsolute(value, label) {
  if (typeof value !== 'string' || !path.isAbsolute(value) || value.startsWith('\\\\') || value.startsWith('\\\\?\\') || value.startsWith('\\\\.\\')) {
    fail(60, 'path-safety', label + ' must be an absolute local path', { value })
  }
  const resolved = path.resolve(value)
  if (resolved.toLowerCase() !== value.toLowerCase()) fail(60, 'path-safety', label + ' must be canonical', { value, resolved })
  return resolved
}
function inside(root, candidate) {
  const relative = path.relative(root, candidate)
  return relative === '' || (relative !== '..' && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative))
}
function safeRelative(value) {
  if (typeof value !== 'string' || !value || value.includes('\\') || value.includes('\0') || path.posix.isAbsolute(value) || path.posix.normalize(value) !== value) return false
  return !value.split('/').some(part => !part || part === '.' || part === '..' || /[<>:"|?*\x00-\x1f]/.test(part) || /[ .]$/.test(part) || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(part))
}
function resolveInside(root, relative) {
  const result = path.resolve(root, ...relative.split('/'))
  if (result === root || !inside(root, result)) fail(62, 'manifest', 'manifest path escapes install root', { relative })
  return result
}
async function sha(file, algorithm, deadline) {
  checkDeadline(deadline, 'hash')
  const hash = createHash(algorithm)
  await new Promise((resolve, reject) => {
    const stream = createReadStream(nativePath(file))
    stream.on('data', chunk => {
      try { checkDeadline(deadline, 'hash'); hash.update(chunk) }
      catch (error) { stream.destroy(error) }
    })
    stream.on('error', reject)
    stream.on('end', resolve)
  })
  return hash.digest('hex')
}
async function parallel(items, action) {
  let cursor = 0
  const results = await Promise.allSettled(Array.from({ length: Math.min(HASH_WORKERS, Math.max(1, items.length)) }, async () => {
    while (cursor < items.length) await action(items[cursor++])
  }))
  const failed = results.find(item => item.status === 'rejected')
  if (failed) throw failed.reason
}
function assertCleanEnvironment() {
  const present = Object.entries(process.env).filter(([name, value]) => value && (BLOCKED_ENV.has(name.toUpperCase()) || name.toUpperCase().startsWith('AGORA_NSIS_'))).map(([name]) => name).sort()
  if (present.length) fail(60, 'environment', 'mutable execution environment was not cleared', { present })
}
async function assertPlainAncestors(root) {
  const chain = []
  let cursor = root
  for (;;) {
    try { await fs.lstat(nativePath(cursor)); chain.push(cursor) }
    catch (error) { if (error.code !== 'ENOENT') throw error }
    const parent = path.dirname(cursor)
    if (parent === cursor) break
    cursor = parent
  }
  for (const entry of chain) {
    const stat = await fs.lstat(nativePath(entry))
    if (!stat.isDirectory() || stat.isSymbolicLink()) fail(60, 'path-safety', 'install root or ancestor is not a plain directory', { entry })
  }
}
function snapshotStat(stat) {
  const size = Number(stat.size), nlink = Number(stat.nlink)
  if (!Number.isSafeInteger(size) || size < 0 || !Number.isSafeInteger(nlink) || nlink < 0) fail(62, 'inventory', 'filesystem metadata exceeds supported integer bounds')
  return { dev: String(stat.dev), ino: String(stat.ino), size, nlink }
}
function sameIdentity(stat, expected) {
  return String(stat.dev) === expected.dev && String(stat.ino) === expected.ino
}
async function inventory(root, deadline) {
  const files = new Map()
  const directories = new Map()
  const rootStat = await fs.lstat(nativePath(root), { bigint: true })
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) fail(62, 'inventory', 'install root is not a plain directory')
  const rootIdentity = snapshotStat(rootStat)
  async function walk(directory, relativeDirectory) {
    checkDeadline(deadline, 'inventory')
    const entries = await fs.readdir(nativePath(directory), { withFileTypes: true })
    entries.sort((a, b) => a.name.localeCompare(b.name, 'en'))
    for (const entry of entries) {
      const absolute = path.join(directory, entry.name)
      const relative = relativeDirectory ? relativeDirectory + '/' + entry.name : entry.name
      if (!safeRelative(relative)) fail(62, 'inventory', 'unsafe filesystem path', { relative })
      const stat = await fs.lstat(nativePath(absolute), { bigint: true })
      if (stat.isSymbolicLink()) fail(62, 'inventory', 'reparse/symbolic-link entries are forbidden', { relative })
      const key = relative.toLowerCase()
      if (files.has(key) || directories.has(key)) fail(62, 'inventory', 'Windows-colliding filesystem path', { relative })
      const snapshot = snapshotStat(stat)
      if (stat.isDirectory()) {
        directories.set(key, { relative, absolute, stat: snapshot })
        await walk(absolute, relative)
      } else if (stat.isFile()) {
        if (stat.nlink !== 1n) fail(62, 'inventory', 'hard-linked installed files are forbidden', { relative, links: snapshot.nlink })
        files.set(key, { relative, absolute, stat: snapshot })
      } else fail(62, 'inventory', 'unsupported filesystem entry type', { relative })
    }
  }
  await walk(root, '')
  return { root: { absolute: root, stat: rootIdentity }, files, directories }
}
function parsePayloadManifest(bytes) {
  let manifest
  try { manifest = JSON.parse(bytes.toString('utf8')) } catch { fail(62, 'manifest', 'payload manifest is not valid JSON') }
  if (manifest.schema !== 'newsroom.desktop-installer-payload/v1' || typeof manifest.productId !== 'string' || !manifest.productId || typeof manifest.archiveSha512 !== 'string' || !/^[a-f0-9]{128}$/.test(manifest.archiveSha512) || !Array.isArray(manifest.files) || !Array.isArray(manifest.directories) || !Array.isArray(manifest.preFiles) || typeof manifest.uninstallerFile !== 'string') {
    fail(62, 'manifest', 'payload manifest identity is invalid')
  }
  const files = new Map()
  const directories = new Map()
  const preFiles = new Map()
  for (const item of manifest.files) {
    if (!item || !safeRelative(item.path) || !Number.isSafeInteger(item.size) || item.size < 0 || typeof item.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(item.sha256)) fail(62, 'manifest', 'payload file declaration is invalid', { item })
    const key = item.path.toLowerCase()
    if (RESERVED_FILES.has(key) || files.has(key) || directories.has(key)) fail(62, 'manifest', 'payload file path collides or is reserved', { path: item.path })
    files.set(key, item)
  }
  for (const relative of manifest.directories) {
    if (!safeRelative(relative)) fail(62, 'manifest', 'payload directory declaration is invalid', { relative })
    const key = relative.toLowerCase()
    if (files.has(key) || directories.has(key)) fail(62, 'manifest', 'payload directory path collides', { relative })
    directories.set(key, relative)
  }
  for (const item of manifest.preFiles) {
    if (!item || !safeRelative(item.path) || item.path.includes('/') || !Number.isSafeInteger(item.size) || item.size < 0 || typeof item.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(item.sha256)) fail(62, 'manifest', 'NSIS pre-file declaration is invalid', { item })
    const key = item.path.toLowerCase()
    if (files.has(key) || directories.has(key) || preFiles.has(key) || RESERVED_FILES.has(key)) fail(62, 'manifest', 'NSIS pre-file path collides', { path: item.path })
    preFiles.set(key, item)
  }
  if (!safeRelative(manifest.uninstallerFile) || manifest.uninstallerFile.includes('/') || files.has(manifest.uninstallerFile.toLowerCase()) || preFiles.has(manifest.uninstallerFile.toLowerCase())) fail(62, 'manifest', 'uninstaller filename is invalid')
  const runtime = manifest.runtime
  if (!runtime || runtime.root !== 'resources/runtime' || runtime.manifest !== 'resources/runtime/desktop-bundle.json' || typeof runtime.manifestSha256 !== 'string' || !/^[a-f0-9]{64}$/.test(runtime.manifestSha256) || !Number.isSafeInteger(runtime.resourceCount) || runtime.resourceCount <= 0) fail(62, 'manifest', 'runtime manifest binding is invalid')
  const runtimeEntry = files.get(runtime.manifest.toLowerCase())
  if (!runtimeEntry || runtimeEntry.sha256 !== runtime.manifestSha256) fail(62, 'manifest', 'runtime manifest is not pinned by payload inventory')
  return { manifest, files, directories, preFiles }
}
async function loadPinnedManifest(file, expectedSha512, productId, deadline) {
  canonicalAbsolute(file, 'payload manifest')
  if (!/^[a-f0-9]{128}$/.test(expectedSha512)) fail(61, 'manifest-pin', 'payload manifest SHA-512 pin is invalid')
  if (await sha(file, 'sha512', deadline) !== expectedSha512) fail(61, 'manifest-pin', 'payload manifest SHA-512 mismatch')
  const bytes = await fs.readFile(nativePath(file))
  const parsed = parsePayloadManifest(bytes)
  if (parsed.manifest.productId !== productId) fail(60, 'ownership', 'payload manifest product identity mismatch')
  return { ...parsed, bytes, sha512: expectedSha512 }
}
async function verifyRuntimeBinding(root, parsed, deadline) {
  const runtimeBytes = await fs.readFile(nativePath(resolveInside(root, parsed.manifest.runtime.manifest)))
  if (createHash('sha256').update(runtimeBytes).digest('hex') !== parsed.manifest.runtime.manifestSha256) fail(62, 'runtime-manifest', 'runtime manifest digest mismatch')
  let runtime
  try { runtime = JSON.parse(runtimeBytes.toString('utf8')) } catch { fail(62, 'runtime-manifest', 'runtime manifest is invalid JSON') }
  if (runtime.schema !== 'newsroom.desktop-app-bundle/v1' || !Array.isArray(runtime.resources) || runtime.resources.length !== parsed.manifest.runtime.resourceCount) fail(62, 'runtime-manifest', 'runtime manifest identity/count mismatch')
  const expected = new Set()
  for (const item of runtime.resources) {
    if (!item || !safeRelative(item.path) || !Number.isSafeInteger(item.size) || typeof item.sha256 !== 'string') fail(62, 'runtime-manifest', 'runtime resource declaration is invalid')
    const payloadPath = parsed.manifest.runtime.root + '/' + item.path
    const payload = parsed.files.get(payloadPath.toLowerCase())
    if (!payload || payload.path !== payloadPath || payload.size !== item.size || payload.sha256 !== item.sha256) fail(62, 'runtime-manifest', 'runtime resource is not identically bound to payload manifest', { path: item.path })
    expected.add(payloadPath.toLowerCase())
  }
  const payloadRuntimeFiles = [...parsed.files.keys()].filter(key => key.startsWith(parsed.manifest.runtime.root.toLowerCase() + '/') && key !== parsed.manifest.runtime.manifest.toLowerCase())
  if (payloadRuntimeFiles.length !== expected.size || payloadRuntimeFiles.some(key => !expected.has(key))) fail(62, 'runtime-manifest', 'runtime payload contains stale or unmanifested files')
  checkDeadline(deadline, 'runtime-manifest')
}
async function verifyPayloadTree(root, parsed, deadline, allowedExtraFiles = new Map()) {
  const actual = await inventory(root, deadline)
  if (actual.directories.size !== parsed.directories.size) fail(62, 'verify', 'payload directory set mismatch', { expected: parsed.directories.size, actual: actual.directories.size })
  for (const [key, relative] of parsed.directories) if (!actual.directories.has(key) || actual.directories.get(key).relative !== relative) fail(62, 'verify', 'payload directory is missing or case-mismatched', { relative })
  const expectedFileCount = parsed.files.size + allowedExtraFiles.size
  if (actual.files.size !== expectedFileCount) fail(62, 'verify', 'payload file set mismatch', { expected: expectedFileCount, actual: actual.files.size })
  const checks = []
  for (const [key, item] of parsed.files) {
    const file = actual.files.get(key)
    if (!file || file.relative !== item.path || file.stat.size !== item.size) fail(62, 'verify', 'payload file missing, case-mismatched or wrong-sized', { path: item.path })
    checks.push({ file, item })
  }
  for (const [key, item] of allowedExtraFiles) {
    const file = actual.files.get(key)
    if (!file || file.relative !== item.path || (Number.isSafeInteger(item.size) && file.stat.size !== item.size)) fail(62, 'verify', 'managed installer file is missing or wrong-sized', { path: item.path })
    if (item.sha256) checks.push({ file, item })
  }
  await parallel(checks, async ({ file, item }) => {
    if (await sha(file.absolute, 'sha256', deadline) !== item.sha256) fail(62, 'verify', 'installed file SHA-256 mismatch', { path: item.path })
  })
  await verifyRuntimeBinding(root, parsed, deadline)
  return actual
}
function extraForPreFiles(parsed) {
  return new Map(parsed.preFiles)
}
async function admitFreshOrOwnedRoot(root, parsed, deadline, allowOwned) {
  canonicalAbsolute(root, 'install root')
  if (root === path.parse(root).root) fail(60, 'path-safety', 'filesystem root cannot be an install root')
  await assertPlainAncestors(root)
  try { await fs.access(nativePath(root)) } catch (error) { if (error.code === 'ENOENT') return { kind: 'missing' }; throw error }
  const tree = await inventory(root, deadline)
  if (tree.files.size === 0 && tree.directories.size === 0) return { kind: 'empty' }
  if (tree.files.has(OWNER_FILE.toLowerCase())) {
    if (!allowOwned) fail(60, 'ownership', 'owned installation still exists at extraction entry')
    await verifyOwnedInstallation(root, parsed.manifest.productId, deadline, { requireSelf: false })
    return { kind: 'owned' }
  }
  if (tree.directories.size !== 0 || tree.files.size !== parsed.preFiles.size) fail(60, 'ownership', 'fresh install root contains unknown entries')
  for (const [key, item] of parsed.preFiles) {
    const file = tree.files.get(key)
    if (!file || file.relative !== item.path || file.stat.size !== item.size || await sha(file.absolute, 'sha256', deadline) !== item.sha256) fail(60, 'ownership', 'NSIS pre-file identity mismatch', { path: item.path })
  }
  return { kind: 'pre-files' }
}
async function verifyOwnedInstallation(root, productId, deadline, { selfPath, requireSelf = true } = {}) {
  const ownerPath = path.join(root, OWNER_FILE)
  const installedManifestPath = path.join(root, INSTALLED_MANIFEST)
  let owner
  try { owner = JSON.parse(await fs.readFile(nativePath(ownerPath), 'utf8')) } catch { fail(60, 'ownership', 'owned installation marker is missing or invalid') }
  if (owner.schema !== 'newsroom.desktop-install-owner/v1' || owner.productId !== productId || owner.installRoot.toLowerCase() !== root.toLowerCase() || owner.sealed !== true || typeof owner.manifestSha512 !== 'string' || !/^[a-f0-9]{128}$/.test(owner.manifestSha512)) fail(60, 'ownership', 'owned installation marker identity changed')
  const parsed = await loadPinnedManifest(installedManifestPath, owner.manifestSha512, productId, deadline)
  const extras = extraForPreFiles(parsed)
  extras.set(OWNER_FILE.toLowerCase(), { path: OWNER_FILE, size: (await fs.lstat(nativePath(ownerPath))).size, sha256: await sha(ownerPath, 'sha256', deadline) })
  extras.set(INSTALLED_MANIFEST.toLowerCase(), { path: INSTALLED_MANIFEST, size: parsed.bytes.length, sha256: createHash('sha256').update(parsed.bytes).digest('hex') })
  if (!owner.uninstaller || owner.uninstaller.path !== parsed.manifest.uninstallerFile || !/^[a-f0-9]{64}$/.test(owner.uninstaller.sha256)) fail(60, 'ownership', 'sealed uninstaller identity is invalid')
  extras.set(owner.uninstaller.path.toLowerCase(), { path: owner.uninstaller.path, sha256: owner.uninstaller.sha256 })
  for (const item of parsed.preFiles.values()) {
    const sealed = owner.preFiles && owner.preFiles[item.path]
    if (sealed !== item.sha256) fail(60, 'ownership', 'sealed NSIS pre-file identity changed', { path: item.path })
  }
  const tree = await verifyPayloadTree(root, parsed, deadline, extras)
  if (requireSelf) {
    const self = canonicalAbsolute(selfPath, 'running uninstaller')
    if (inside(root, self)) fail(60, 'ownership', 'uninstaller must execute from outside the owned install root')
    if (await sha(self, 'sha256', deadline) !== owner.uninstaller.sha256) fail(60, 'ownership', 'running uninstaller digest differs from sealed installed uninstaller')
  }
  return { owner, parsed, tree }
}
async function runFile(file, args, { deadline, limitMs, maxBuffer = 16 * 1024 * 1024, stdoutParser } = {}) {
  canonicalAbsolute(file, 'child executable')
  checkDeadline(deadline, 'child-start')
  const timeout = Math.max(1, Math.min(limitMs, deadline - Date.now()))
  return new Promise((resolve, reject) => {
    const child = spawn(file, args, { windowsHide: true, shell: false, env: {}, stdio: ['ignore', 'pipe', 'pipe'] })
    activeChildren.add(child)
    const stdout = [], stderr = []
    let total = 0, failure
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL') }, timeout)
    const stop = error => { if (!failure) { failure = error; child.kill('SIGKILL') } }
    const collect = (target, parse) => chunk => {
      if (failure || timedOut) return
      try {
        checkDeadline(deadline, 'child-output')
        if (parse) { parse.push(chunk); return }
        total += chunk.length
        if (total > maxBuffer) fail(61, 'child-output', 'child output exceeded fixed budget')
        target.push(chunk)
      } catch (error) { stop(error) }
    }
    child.stdout.on('data', collect(stdout, stdoutParser)); child.stderr.on('data', collect(stderr))
    child.on('error', error => { activeChildren.delete(child); clearTimeout(timer); reject(error) })
    child.on('close', code => {
      activeChildren.delete(child)
      clearTimeout(timer)
      // Settle only after close: a rejected parser must not leave a running decoder behind.
      if (failure) { reject(failure); return }
      if (timedOut) { reject(new InstallerError(124, 'child-timeout', 'child process exceeded fixed total budget')); return }
      const result = { code, stdout: Buffer.concat(stdout).toString('utf8'), stderr: Buffer.concat(stderr).toString('utf8') }
      if (code !== 0) { reject(new InstallerError(61, 'child-exit', 'child process failed', { code, stderr: result.stderr.slice(-2048) })); return }
      try { if (stdoutParser) result.inventory = stdoutParser.finish(); resolve(result) }
      catch (error) { reject(error) }
    })
  })
}
function archiveRelative(value) {
  if (typeof value !== 'string' || !value || /^[\\/]/.test(value) || /^[a-z]:/i.test(value)) fail(61, 'archive-list', 'archive contains an absolute path', { value })
  const normalized = value.replaceAll('\\', '/')
  if (!safeRelative(normalized)) fail(61, 'archive-list', 'archive contains an unsafe path', { value })
  return normalized
}
// The pinned 7za -slt -ba UTF-8 grammar is shared by packaging and this standalone
// bootstrap. Keep only a bounded line/record and a manifest-bounded seen set, never
// aggregate stdout or materialize a second full member inventory.
const LISTING_FIELDS = new Set(['Path', 'Size', 'Packed Size', 'Modified', 'Created', 'Accessed', 'Attributes', 'CRC', 'Encrypted', 'Method', 'Block', 'Folder'])
const LISTING_METADATA_BYTES = 1024
const LISTING_MAX_PATH_BYTES = 128 * 1024
function createArchiveInventoryParser(parsed) {
  const expectedCount = parsed.files.size + parsed.directories.size
  let pathBytes = 0, maxPathBytes = 0
  for (const entries of [parsed.files, parsed.directories]) for (const [key, value] of entries) {
    const relative = typeof value === 'string' ? value : value.path
    if (!safeRelative(relative) || key !== relative.toLowerCase()) fail(61, 'archive-list', 'invalid expected inventory path')
    const size = Buffer.byteLength(relative, 'utf8')
    if (size > LISTING_MAX_PATH_BYTES) fail(61, 'archive-list', 'expected inventory path exceeds supported byte budget')
    pathBytes += size; maxPathBytes = Math.max(maxPathBytes, size)
  }
  const maxBytes = pathBytes + expectedCount * LISTING_METADATA_BYTES + 2
  const maxLineBytes = Math.max(LISTING_METADATA_BYTES, maxPathBytes + 7)
  if (!Number.isSafeInteger(maxBytes) || expectedCount < 1) fail(61, 'archive-list', 'invalid manifest-derived archive listing budget')
  const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true })
  const seen = new Set()
  let fields = new Map(), recordBytes = 0, pending = Buffer.alloc(0)
  let listingBytes = 0, files = 0, directories = 0, finished = false, failed = false
  const rejectListing = (message, details) => fail(61, 'archive-list', message, details)
  function endRecord() {
    if (!fields.size) return
    if (!fields.has('Path') || !fields.has('Size') || !fields.has('Attributes') || fields.get('Encrypted') !== '-') rejectListing('archive listing record is incomplete or encrypted')
    const relative = archiveRelative(fields.get('Path')), key = relative.toLowerCase()
    if (seen.has(key)) rejectListing('archive contains duplicate or Windows-colliding paths', { path: relative })
    if (seen.size >= expectedCount) rejectListing('archive inventory count exceeds payload manifest')
    const directory = fields.get('Attributes').startsWith('D')
    if (fields.has('Folder') && fields.get('Folder') !== (directory ? '+' : '-')) rejectListing('archive listing has conflicting directory metadata', { path: relative })
    if (!/^(0|[1-9][0-9]*)$/.test(fields.get('Size'))) rejectListing('archive listing has invalid member size', { path: relative })
    const size = Number(fields.get('Size'))
    if (!Number.isSafeInteger(size)) rejectListing('archive member size exceeds supported integer bounds', { path: relative })
    const expected = directory ? parsed.directories.get(key) : parsed.files.get(key)
    if ((directory ? expected : expected && expected.path) !== relative) rejectListing('archive inventory count/path/type differs from payload manifest', { path: relative })
    if (size !== (directory ? 0 : expected.size)) rejectListing('archive member size differs from payload manifest', { path: relative })
    if (recordBytes > Buffer.byteLength(fields.get('Path'), 'utf8') + LISTING_METADATA_BYTES) rejectListing('archive listing record exceeded metadata budget')
    seen.add(key)
    if (directory) directories++; else files++
    fields = new Map(); recordBytes = 0
  }
  function line(bytes) {
    if (bytes.length > maxLineBytes + 1) rejectListing('archive listing line exceeded manifest-derived byte budget')
    if (bytes.at(-1) === 13) bytes = bytes.subarray(0, -1)
    if (bytes.length > maxLineBytes) rejectListing('archive listing line exceeded manifest-derived byte budget')
    let text
    try { text = decoder.decode(bytes) } catch { rejectListing('archive listing is not valid UTF-8') }
    if (/[\x00-\x1f\x7f]/.test(text)) rejectListing('archive listing contains control characters')
    if (!text) { endRecord(); return }
    const at = text.indexOf(' = '), key = text.slice(0, at)
    if (at <= 0 || !LISTING_FIELDS.has(key)) rejectListing('archive listing has an unexpected field or line')
    if (fields.has(key)) rejectListing('archive listing contains a duplicate field', { field: key })
    recordBytes += bytes.length + 2
    if (recordBytes > maxPathBytes + LISTING_METADATA_BYTES) rejectListing('archive listing record exceeded metadata budget')
    fields.set(key, text.slice(at + 3))
  }
  return {
    limits: Object.freeze({ expectedCount, maxBytes, maxLineBytes }),
    push(chunk) {
      if (finished || failed) rejectListing('archive listing parser is already closed')
      try {
        if (!Buffer.isBuffer(chunk)) rejectListing('archive listing must be supplied as raw bytes')
        listingBytes += chunk.length
        if (listingBytes > maxBytes) rejectListing('archive listing exceeded manifest-derived total byte budget')
        let start = 0
        for (let at = chunk.indexOf(10); at !== -1; at = chunk.indexOf(10, start)) {
          const part = chunk.subarray(start, at)
          if (pending.length + part.length > maxLineBytes + 1) rejectListing('archive listing line exceeded manifest-derived byte budget')
          line(pending.length ? Buffer.concat([pending, part]) : part)
          pending = Buffer.alloc(0); start = at + 1
        }
        const tail = chunk.subarray(start)
        if (pending.length + tail.length > maxLineBytes + 1) rejectListing('archive listing line exceeded manifest-derived byte budget')
        // Copy the tail so a small partial line cannot retain a large upstream chunk.
        pending = Buffer.concat([pending, tail])
      } catch (error) { failed = true; throw error }
    },
    finish() {
      if (finished || failed) rejectListing('archive listing parser is already closed')
      try {
        if (pending.length) line(pending)
        pending = Buffer.alloc(0); endRecord()
        if (files !== parsed.files.size || directories !== parsed.directories.size) rejectListing('archive inventory count differs from payload manifest', { files, directories })
        finished = true
        return { listingBytes, members: seen.size, files, directories, limits: this.limits }
      } catch (error) { failed = true; throw error }
    }
  }
}
async function verifyArchiveListing(extractor, archive, parsed, deadline) {
  const stdoutParser = createArchiveInventoryParser(parsed)
  const result = await runFile(extractor, ['l', '-slt', '-ba', '-sccUTF-8', '--', archive], { deadline, limitMs: EXTRACT_BUDGET_MS, maxBuffer: 64 * 1024, stdoutParser })
  return result.inventory
}
async function verifyArchive(extractor, archive, extractorSha256, archiveSha512, parsed, deadline) {
  if (!/^[a-f0-9]{64}$/.test(extractorSha256) || await sha(extractor, 'sha256', deadline) !== extractorSha256) fail(61, 'extractor-pin', 'modern decoder SHA-256 mismatch')
  if (!/^[a-f0-9]{128}$/.test(archiveSha512) || archiveSha512 !== parsed.manifest.archiveSha512 || await sha(archive, 'sha512', deadline) !== archiveSha512) fail(61, 'archive-pin', 'payload archive SHA-512 mismatch')
  return verifyArchiveListing(extractor, archive, parsed, deadline)
}
async function installPayload(extractor, archive, root, parsed, deadline) {
  await admitFreshOrOwnedRoot(root, parsed, deadline, false)
  await fs.mkdir(nativePath(root), { recursive: true })
  await runFile(extractor, ['x', '-y', '-aoa', '-bd', '-bso0', '-bse0', '-bsp0', '-o' + root, '--', archive], { deadline, limitMs: EXTRACT_BUDGET_MS, maxBuffer: 64 * 1024 })
  await verifyPayloadTree(root, parsed, deadline, extraForPreFiles(parsed))
  const installedManifest = path.join(root, INSTALLED_MANIFEST)
  const ownerPath = path.join(root, OWNER_FILE)
  await fs.writeFile(nativePath(installedManifest), parsed.bytes, { flag: 'wx' })
  const owner = { schema: 'newsroom.desktop-install-owner/v1', productId: parsed.manifest.productId, installRoot: root, manifestSha512: parsed.sha512, sealed: false }
  await fs.writeFile(nativePath(ownerPath), JSON.stringify(owner, null, 2) + '\n', { flag: 'wx' })
}
async function sealInstallation(root, parsed, deadline) {
  const ownerPath = path.join(root, OWNER_FILE)
  let owner
  try { owner = JSON.parse(await fs.readFile(nativePath(ownerPath), 'utf8')) } catch { fail(60, 'seal', 'unsealed owner marker is missing or invalid') }
  if (owner.schema !== 'newsroom.desktop-install-owner/v1' || owner.productId !== parsed.manifest.productId || owner.installRoot.toLowerCase() !== root.toLowerCase() || owner.manifestSha512 !== parsed.sha512 || owner.sealed !== false) fail(60, 'seal', 'unsealed owner identity changed')
  const installedManifest = path.join(root, INSTALLED_MANIFEST)
  if (await sha(installedManifest, 'sha512', deadline) !== parsed.sha512) fail(60, 'seal', 'installed payload manifest identity changed')
  const uninstallerPath = resolveInside(root, parsed.manifest.uninstallerFile)
  const uninstallerStat = await fs.lstat(nativePath(uninstallerPath))
  if (!uninstallerStat.isFile() || uninstallerStat.isSymbolicLink() || uninstallerStat.nlink !== 1) fail(60, 'seal', 'installed uninstaller is not a plain independent file')
  const extras = extraForPreFiles(parsed)
  extras.set(OWNER_FILE.toLowerCase(), { path: OWNER_FILE, size: (await fs.lstat(nativePath(ownerPath))).size, sha256: await sha(ownerPath, 'sha256', deadline) })
  extras.set(INSTALLED_MANIFEST.toLowerCase(), { path: INSTALLED_MANIFEST, size: parsed.bytes.length, sha256: createHash('sha256').update(parsed.bytes).digest('hex') })
  extras.set(parsed.manifest.uninstallerFile.toLowerCase(), { path: parsed.manifest.uninstallerFile, sha256: await sha(uninstallerPath, 'sha256', deadline) })
  await verifyPayloadTree(root, parsed, deadline, extras)
  owner = { ...owner, sealed: true, uninstaller: { path: parsed.manifest.uninstallerFile, sha256: extras.get(parsed.manifest.uninstallerFile.toLowerCase()).sha256 }, preFiles: Object.fromEntries([...parsed.preFiles.values()].map(item => [item.path, item.sha256])) }
  await fs.writeFile(nativePath(ownerPath), JSON.stringify(owner, null, 2) + '\n', { flag: 'w' })
  await verifyOwnedInstallation(root, parsed.manifest.productId, deadline, { requireSelf: false })
}
async function planOwnedInstallationDeletion(root, productId, selfPath, deadline) {
  const verified = await verifyOwnedInstallation(root, productId, deadline, { selfPath, requireSelf: true })
  return { root, tree: verified.tree }
}
async function revalidateDeletionEntry(plan, entry, kind, deadline, progress) {
  checkDeadline(deadline, 'delete-revalidate')
  const rootStat = await fs.lstat(nativePath(plan.root), { bigint: true })
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink() || !sameIdentity(rootStat, plan.tree.root.stat)) fail(70, 'delete-identity', 'owned install root identity changed during deletion', progress)
  const parts = entry.relative.split('/')
  let relative = ''
  let absolute = plan.root
  for (const part of parts.slice(0, -1)) {
    relative = relative ? relative + '/' + part : part
    absolute = path.join(absolute, part)
    const expected = plan.tree.directories.get(relative.toLowerCase())
    if (!expected || expected.relative !== relative) fail(70, 'delete-identity', 'deletion ancestor left the verified tree', { ...progress, path: relative })
    const stat = await fs.lstat(nativePath(absolute), { bigint: true })
    if (!stat.isDirectory() || stat.isSymbolicLink() || !sameIdentity(stat, expected.stat)) fail(70, 'delete-identity', 'deletion ancestor identity changed', { ...progress, path: relative })
  }
  const stat = await fs.lstat(nativePath(entry.absolute), { bigint: true })
  const expectedType = kind === 'file' ? stat.isFile() : stat.isDirectory()
  if (!expectedType || stat.isSymbolicLink() || !sameIdentity(stat, entry.stat)) fail(70, 'delete-identity', 'deletion entry identity changed', { ...progress, path: entry.relative })
  if (kind === 'file' && (stat.nlink !== 1n || Number(stat.size) !== entry.stat.size)) fail(70, 'delete-identity', 'deletion file link/size identity changed', { ...progress, path: entry.relative })
}
async function executeOwnedInstallationDeletion(plan, deadline) {
  const progress = { deletedFiles: 0, deletedDirectories: 0 }
  const files = [...plan.tree.files.values()].sort((a, b) => b.relative.length - a.relative.length || b.relative.localeCompare(a.relative, 'en'))
  for (const file of files) {
    await revalidateDeletionEntry(plan, file, 'file', deadline, progress)
    await fs.unlink(nativePath(file.absolute))
    progress.deletedFiles += 1
  }
  const directories = [...plan.tree.directories.values()].sort((a, b) => b.relative.split('/').length - a.relative.split('/').length || b.relative.length - a.relative.length || b.relative.localeCompare(a.relative, 'en'))
  for (const directory of directories) {
    await revalidateDeletionEntry(plan, directory, 'directory', deadline, progress)
    await fs.rmdir(nativePath(directory.absolute))
    progress.deletedDirectories += 1
  }
  checkDeadline(deadline, 'delete-root')
  const rootStat = await fs.lstat(nativePath(plan.root), { bigint: true })
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink() || !sameIdentity(rootStat, plan.tree.root.stat)) fail(70, 'delete-identity', 'owned install root identity changed before final removal', progress)
  await fs.rmdir(nativePath(plan.root))
  try { await fs.access(nativePath(plan.root)); fail(70, 'delete', 'owned install root remains after deletion', progress) } catch (error) { if (error instanceof InstallerError) throw error; if (error.code !== 'ENOENT') throw error }
  return progress
}
async function deleteOwnedInstallation(root, productId, selfPath, deadline) {
  const plan = await planOwnedInstallationDeletion(root, productId, selfPath, deadline)
  return executeOwnedInstallationDeletion(plan, deadline)
}
function writeResult(resultPath, value) {
  try { require('node:fs').writeFileSync(nativePath(resultPath), JSON.stringify(value) + '\n', { flag: 'w' }) } catch {}
}
async function main(argv = process.argv.slice(2)) {
  const mode = argv[0]
  const started = Date.now()
  const deadline = started + TOTAL_BUDGET_MS
  let resultPath
  const hardTimer = setTimeout(() => {
    writeResult(resultPath, { ok: false, code: 124, phase: 'deadline' })
    for (const child of activeChildren) child.kill('SIGKILL')
    setTimeout(() => process.exit(124), 2000)
  }, TOTAL_BUDGET_MS)
  hardTimer.unref()
  try {
    const candidateResult = argv[argv.length - 1]
    if (typeof candidateResult === 'string' && path.isAbsolute(candidateResult) && !candidateResult.startsWith('\\\\')) resultPath = path.resolve(candidateResult)
    assertCleanEnvironment()
    if (mode === 'preflight' || mode === 'install') {
      if (argv.length !== 10) fail(60, 'arguments', 'invalid fixed install bootstrap arguments', { count: argv.length })
      const [, extractorArg, extractorSha256, archiveArg, archiveSha512, manifestArg, manifestSha512, rootArg, productId, resultArg] = argv
      const extractor = canonicalAbsolute(extractorArg, 'extractor'), archive = canonicalAbsolute(archiveArg, 'archive'), root = canonicalAbsolute(rootArg, 'install root')
      resultPath = canonicalAbsolute(resultArg, 'result path')
      const parsed = await loadPinnedManifest(manifestArg, manifestSha512, productId, deadline)
      await verifyArchive(extractor, archive, extractorSha256, archiveSha512, parsed, deadline)
      const admission = await admitFreshOrOwnedRoot(root, parsed, deadline, mode === 'preflight')
      if (mode === 'install') await installPayload(extractor, archive, root, parsed, deadline)
      writeResult(resultPath, { ok: true, mode, admission: admission.kind, elapsedMs: Date.now() - started })
    } else if (mode === 'admit-owned') {
      if (argv.length !== 4) fail(60, 'arguments', 'invalid owned-install admission arguments', { count: argv.length })
      const [, rootArg, productId, resultArg] = argv
      const root = canonicalAbsolute(rootArg, 'owned install root'); resultPath = canonicalAbsolute(resultArg, 'result path')
      await assertPlainAncestors(root)
      await verifyOwnedInstallation(root, productId, deadline, { requireSelf: false })
      writeResult(resultPath, { ok: true, mode, elapsedMs: Date.now() - started })
    } else if (mode === 'seal') {
      if (argv.length !== 6) fail(60, 'arguments', 'invalid fixed seal arguments', { count: argv.length })
      const [, manifestArg, manifestSha512, rootArg, productId, resultArg] = argv
      const root = canonicalAbsolute(rootArg, 'install root'); resultPath = canonicalAbsolute(resultArg, 'result path')
      const parsed = await loadPinnedManifest(manifestArg, manifestSha512, productId, deadline)
      await sealInstallation(root, parsed, deadline)
      writeResult(resultPath, { ok: true, mode, elapsedMs: Date.now() - started })
    } else if (mode === 'uninstall') {
      if (argv.length !== 7) fail(60, 'arguments', 'invalid fixed uninstall arguments', { count: argv.length })
      const [, manifestArg, manifestSha512, rootArg, productId, selfArg, resultArg] = argv
      const root = canonicalAbsolute(rootArg, 'install root'); resultPath = canonicalAbsolute(resultArg, 'result path')
      await loadPinnedManifest(manifestArg, manifestSha512, productId, deadline)
      await deleteOwnedInstallation(root, productId, selfArg, deadline)
      writeResult(resultPath, { ok: true, mode, elapsedMs: Date.now() - started })
    } else fail(60, 'arguments', 'unknown bootstrap mode', { mode })
    clearTimeout(hardTimer)
  } catch (error) {
    clearTimeout(hardTimer)
    const normalized = error instanceof InstallerError ? error : new InstallerError(70, 'unexpected', String(error && error.stack ? error.stack : error))
    writeResult(resultPath, { ok: false, code: normalized.code, phase: normalized.phase, message: normalized.message, details: normalized.details })
    process.exitCode = normalized.code
  }
}

module.exports = { InstallerError, TOTAL_BUDGET_MS, deleteOwnedInstallation, executeOwnedInstallationDeletion, parsePayloadManifest, createArchiveInventoryParser, verifyArchiveListing, verifyArchive, planOwnedInstallationDeletion, runFile, main }
if (require.main === module) void main()
