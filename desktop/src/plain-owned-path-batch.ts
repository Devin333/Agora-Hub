import { lstat, readdir, realpath } from "node:fs/promises"
import path from "node:path"
import { inside } from "../shared/plain-owned-path.cjs"

export interface PlainOwnedPathStats {
  readonly dev: number | bigint
  readonly ino: number | bigint
  readonly mtimeNs?: bigint
  readonly ctimeNs?: bigint
  isDirectory(): boolean
  isSymbolicLink(): boolean
}

export interface PlainOwnedPathDirent {
  readonly name: string
  isFile(): boolean
  isDirectory(): boolean
  isSymbolicLink(): boolean
}

export interface PlainOwnedPathIO {
  lstat(candidate: string): Promise<PlainOwnedPathStats>
  realpath(candidate: string): Promise<string>
  readdir(candidate: string): Promise<PlainOwnedPathDirent[]>
}

export interface PlainOwnedPathCandidate {
  path: string
  label: string
}

interface CandidateNode {
  component: string
  label: string
  children: Map<string, CandidateNode>
}

const defaultIO: PlainOwnedPathIO = {
  lstat: candidate => lstat(candidate, { bigint: true }),
  realpath,
  readdir: candidate => readdir(candidate, { withFileTypes: true })
}

function pathKey(component: string): string {
  return process.platform === "win32" ? component.toLowerCase() : component
}

function chainTo(candidate: string): string[] {
  const resolved = path.resolve(candidate)
  const volumeRoot = path.parse(resolved).root
  const chain = [volumeRoot]
  let current = volumeRoot
  for (const component of path.relative(volumeRoot, resolved).split(path.sep).filter(Boolean)) {
    current = path.join(current, component)
    chain.push(current)
  }
  return chain
}

function sameOptionalTimestamp(left: bigint | undefined, right: bigint | undefined): boolean {
  return (left === undefined && right === undefined) || (left !== undefined && right !== undefined && left === right)
}

function sameIdentity(left: PlainOwnedPathStats, right: PlainOwnedPathStats): boolean {
  return left.dev === right.dev && left.ino === right.ino &&
    sameOptionalTimestamp(left.mtimeNs, right.mtimeNs) && sameOptionalTimestamp(left.ctimeNs, right.ctimeNs) &&
    left.isDirectory() === right.isDirectory() && left.isSymbolicLink() === right.isSymbolicLink()
}

export class PlainOwnedPathBatchError extends Error {
  constructor(readonly code: "path_outside_root" | "path_missing" | "path_untrusted", readonly label: string, message: string) {
    super(message)
    this.name = "PlainOwnedPathBatchError"
  }
}

function fail(code: PlainOwnedPathBatchError["code"], label: string, message: string): never {
  throw new PlainOwnedPathBatchError(code, label, message)
}

async function initialLstat(io: PlainOwnedPathIO, candidate: string, label: string): Promise<PlainOwnedPathStats> {
  let info: PlainOwnedPathStats
  try { info = await io.lstat(candidate) }
  catch { fail("path_missing", label, `${label} is missing`) }
  if (info.isSymbolicLink()) fail("path_untrusted", label, `${label} traverses a symbolic link or junction`)
  return info
}

async function unchangedDirectory(io: PlainOwnedPathIO, candidate: string, label: string, before: PlainOwnedPathStats): Promise<void> {
  let after: PlainOwnedPathStats
  try { after = await io.lstat(candidate) }
  catch { fail("path_untrusted", label, `${label} changed while the runtime payload was being validated`) }
  if (after.isSymbolicLink() || !sameIdentity(before, after)) {
    fail("path_untrusted", label, `${label} changed while the runtime payload was being validated`)
  }
}

function candidateTree(resolvedRoot: string, candidates: readonly PlainOwnedPathCandidate[], canonicalRoot: string, rootLabel: string): CandidateNode {
  const root: CandidateNode = { component: "", label: rootLabel, children: new Map() }
  for (const candidate of candidates) {
    const resolvedCandidate = path.resolve(candidate.path)
    if (!inside(resolvedRoot, resolvedCandidate)) fail("path_outside_root", candidate.label, `${candidate.label} escapes the owned root`)
    const relative = path.relative(resolvedRoot, resolvedCandidate)
    const canonicalCandidate = path.resolve(canonicalRoot, relative)
    if (!inside(canonicalRoot, canonicalCandidate)) fail("path_untrusted", candidate.label, `${candidate.label} resolves outside the owned root`)

    let node = root
    for (const component of relative.split(path.sep).filter(Boolean)) {
      const key = pathKey(component)
      let child = node.children.get(key)
      if (!child) {
        child = { component, label: candidate.label, children: new Map() }
        node.children.set(key, child)
      }
      node = child
    }
  }
  return root
}

async function validateChildren(io: PlainOwnedPathIO, parentPath: string, parent: CandidateNode): Promise<void> {
  let entries: PlainOwnedPathDirent[]
  try { entries = await io.readdir(parentPath) }
  catch { fail("path_missing", parent.label, `${parent.label} is missing`) }
  const entriesByName = new Map(entries.map(entry => [pathKey(entry.name), entry]))

  for (const child of parent.children.values()) {
    const entry = entriesByName.get(pathKey(child.component))
    if (!entry) fail("path_missing", child.label, `${child.label} is missing`)
    if (entry.isSymbolicLink()) fail("path_untrusted", child.label, `${child.label} traverses a symbolic link or junction`)

    const childPath = path.join(parentPath, entry.name)
    let before: PlainOwnedPathStats | undefined
    if (!entry.isFile()) before = await initialLstat(io, childPath, child.label)
    if (child.children.size === 0) continue
    if (!(before?.isDirectory() ?? entry.isDirectory())) fail("path_missing", child.label, `${child.label} is missing`)

    before ??= await initialLstat(io, childPath, child.label)
    if (!before.isDirectory()) fail("path_missing", child.label, `${child.label} is missing`)
    await validateChildren(io, childPath, child)
    await unchangedDirectory(io, childPath, child.label, before)
  }
}

/**
 * Validates many owned paths as one directory walk. Each traversed directory is
 * read once, while its identity is checked before and after its children so a
 * replacement cannot silently change the walk. Leaf entries need no per-file I/O.
 *
 * Canonical containment does not require a realpath per leaf: after the root's
 * lexical chain and every descendant component have been proven link-free, a
 * descendant's canonical location is the same relative path below canonicalRoot.
 */
export async function assertPlainOwnedPaths(
  root: string,
  candidates: readonly PlainOwnedPathCandidate[],
  options: { io?: PlainOwnedPathIO; rootLabel?: string } = {}
): Promise<void> {
  const io = options.io ?? defaultIO
  const rootLabel = options.rootLabel ?? "resource root"
  const resolvedRoot = path.resolve(root)

  let rootInfo: PlainOwnedPathStats | undefined
  for (const lexical of chainTo(resolvedRoot)) {
    const info = await initialLstat(io, lexical, rootLabel)
    if (lexical === resolvedRoot) rootInfo = info
  }
  if (!rootInfo?.isDirectory()) fail("path_untrusted", rootLabel, `${rootLabel} must be a directory`)
  const validatedRootInfo = rootInfo

  let canonicalRoot: string
  try { canonicalRoot = await io.realpath(resolvedRoot) }
  catch { fail("path_missing", rootLabel, `${rootLabel} is missing`) }
  const tree = candidateTree(resolvedRoot, candidates, canonicalRoot, rootLabel)
  await validateChildren(io, resolvedRoot, tree)
  await unchangedDirectory(io, resolvedRoot, rootLabel, validatedRootInfo)

  let finalCanonicalRoot: string
  try { finalCanonicalRoot = await io.realpath(resolvedRoot) }
  catch { fail("path_untrusted", rootLabel, `${rootLabel} changed while the runtime payload was being validated`) }
  if (pathKey(path.normalize(finalCanonicalRoot)) !== pathKey(path.normalize(canonicalRoot))) {
    fail("path_untrusted", rootLabel, `${rootLabel} changed while the runtime payload was being validated`)
  }
}
