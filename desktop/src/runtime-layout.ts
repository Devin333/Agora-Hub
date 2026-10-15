import { readFile } from "node:fs/promises"
import path from "node:path"
import { assertPlainOwnedPath as assertSharedPlainOwnedPath, inside } from "../shared/plain-owned-path.cjs"
import { assertPlainOwnedPaths as assertBatchPlainOwnedPaths, PlainOwnedPathBatchError } from "./plain-owned-path-batch"

const DEV_MANIFEST_SCHEMA = "agora.desktop-runtime-manifest.v1"
const DEV_MANIFEST_OWNER = "agora-paper-agent-desktop"
const BUNDLE_SCHEMA = "newsroom.desktop-app-bundle/v1"

interface DevelopmentManifest {
  schema: typeof DEV_MANIFEST_SCHEMA
  owner: typeof DEV_MANIFEST_OWNER
  mode: "development"
  rootRelativePath: string
  pythonRelativePath: string
  entryModule: "scripts.desktop_app"
  requiredPaths: string[]
}

export interface RuntimeLayout {
  resourceRoot: string
  dataRoot: string
  pythonPath: string
  manifestPath: string
  entryModule: "scripts.desktop_app"
  mode: "development" | "packaged"
}

export interface RuntimeLayoutInput {
  isPackaged: boolean
  appPath: string
  resourcesPath: string
  userDataPath: string
}

export class RuntimeLayoutError extends Error {
  constructor(readonly code: string, message: string) {
    super(message)
    this.name = "RuntimeLayoutError"
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
}

function safeRelativePath(value: string): boolean {
  if (!value || value.includes("\\") || path.isAbsolute(value)) return false
  return !value.split("/").some(part => !part || part === "." || part === ".." || part.includes(":") || part.endsWith(" ") || part.endsWith("."))
}

function parseDevelopmentManifest(value: unknown): DevelopmentManifest {
  if (!isRecord(value)) throw new RuntimeLayoutError("runtime_manifest_invalid", "Runtime manifest must be an object")
  const requiredPaths = value.requiredPaths
  if (
    value.schema !== DEV_MANIFEST_SCHEMA || value.owner !== DEV_MANIFEST_OWNER || value.mode !== "development" ||
    typeof value.rootRelativePath !== "string" || typeof value.pythonRelativePath !== "string" || !safeRelativePath(value.pythonRelativePath) || value.entryModule !== "scripts.desktop_app" ||
    !Array.isArray(requiredPaths) || requiredPaths.length === 0 || requiredPaths.some(item => typeof item !== "string" || !safeRelativePath(item))
  ) throw new RuntimeLayoutError("runtime_manifest_invalid", "Development runtime manifest fields are invalid")
  return value as unknown as DevelopmentManifest
}

function parseBundleManifest(value: unknown): { pythonRelativePath: string; requiredPaths: string[] } {
  if (!isRecord(value) || value.schema !== BUNDLE_SCHEMA || !Array.isArray(value.resources) || value.resources.length === 0 || !isRecord(value.runtime) || !isRecord(value.runtime.python) || !isRecord(value.runtime.node)) {
    throw new RuntimeLayoutError("runtime_manifest_invalid", "Packaged desktop-bundle manifest is invalid")
  }
  if (value.runtime.python.path !== ".venv/Scripts/python.exe" || value.runtime.node.path !== "node/node.exe") {
    throw new RuntimeLayoutError("runtime_manifest_invalid", "Packaged runtime executable layout is not allowed")
  }
  const resources: string[] = []
  const resourcePaths = new Set<string>()
  for (const item of value.resources) {
    if (!isRecord(item) || typeof item.path !== "string" || !safeRelativePath(item.path)) {
      throw new RuntimeLayoutError("runtime_manifest_invalid", "Packaged resource inventory contains an unsafe path")
    }
    if (resourcePaths.has(item.path)) throw new RuntimeLayoutError("runtime_manifest_invalid", "Packaged resource inventory contains a duplicate path")
    resourcePaths.add(item.path)
    resources.push(item.path)
  }
  const hasEntry = resourcePaths.has("scripts/desktop_app.py") || resourcePaths.has("scripts/desktop_app/__main__.py")
  if (!hasEntry) throw new RuntimeLayoutError("runtime_entry_missing", "Packaged resource inventory does not contain scripts.desktop_app")
  return { pythonRelativePath: value.runtime.python.path, requiredPaths: [value.runtime.python.path, value.runtime.node.path, ...resources] }
}

async function assertPlainOwnedPath(root: string, candidate: string, label: string): Promise<void> {
  try { await assertSharedPlainOwnedPath(root, candidate, { label }) }
  catch (error) {
    const code = error && typeof error === "object" && "code" in error ? String(error.code) : "path_untrusted"
    if (code === "path_outside_root") throw new RuntimeLayoutError("runtime_path_outside_root", `${label} escapes the owned runtime root`)
    if (code === "path_missing") throw new RuntimeLayoutError("runtime_path_missing", `${label} is missing from the runtime payload`)
    throw new RuntimeLayoutError("runtime_path_untrusted", `${label} must not traverse a symbolic link or junction`)
  }
}
async function assertRequiredPaths(root: string, requiredPaths: readonly string[]): Promise<void> {
  try {
    await assertBatchPlainOwnedPaths(root, requiredPaths.map(requiredPath => ({
      path: path.join(root, requiredPath),
      label: requiredPath
    })))
  } catch (error) {
    const code = error && typeof error === "object" && "code" in error ? String(error.code) : "path_untrusted"
    const label = error instanceof PlainOwnedPathBatchError ? error.label : "runtime path"
    if (code === "path_outside_root") throw new RuntimeLayoutError("runtime_path_outside_root", `${label} escapes the owned runtime root`)
    if (code === "path_missing") throw new RuntimeLayoutError("runtime_path_missing", `${label} is missing from the runtime payload`)
    throw new RuntimeLayoutError("runtime_path_untrusted", `${label} must not traverse a symbolic link or junction`)
  }
}

async function readJson(manifestPath: string): Promise<unknown> {
  try { return JSON.parse(await readFile(manifestPath, "utf8")) }
  catch (error) {
    if (error instanceof SyntaxError) throw new RuntimeLayoutError("runtime_manifest_invalid", "Runtime manifest is not valid JSON")
    throw new RuntimeLayoutError("runtime_manifest_missing", "Owned runtime manifest is missing")
  }
}

export async function resolveRuntimeLayout(input: RuntimeLayoutInput): Promise<RuntimeLayout> {
  const mode = input.isPackaged ? "packaged" : "development"
  const manifestPath = input.isPackaged
    ? path.join(input.resourcesPath, "runtime", "desktop-bundle.json")
    : path.join(input.appPath, "runtime-manifest.dev.json")
  const resourceRoot = input.isPackaged ? path.resolve(input.resourcesPath, "runtime") : path.resolve(input.appPath, "..")
  await assertPlainOwnedPath(resourceRoot, resourceRoot, "resource root")
  await assertPlainOwnedPath(resourceRoot, manifestPath, "runtime manifest")
  const manifestValue = await readJson(manifestPath)
  const descriptor = input.isPackaged
    ? parseBundleManifest(manifestValue)
    : (() => {
        const manifest = parseDevelopmentManifest(manifestValue)
        if (path.resolve(path.dirname(manifestPath), manifest.rootRelativePath) !== resourceRoot) throw new RuntimeLayoutError("runtime_root_not_owned", "Development manifest does not resolve to the expected workspace root")
        return { pythonRelativePath: manifest.pythonRelativePath, requiredPaths: manifest.requiredPaths }
      })()

  await assertRequiredPaths(resourceRoot, [...descriptor.requiredPaths, descriptor.pythonRelativePath])
  const pythonPath = path.resolve(resourceRoot, descriptor.pythonRelativePath)
  await assertPlainOwnedPath(resourceRoot, pythonPath, "Python interpreter")
  const dataRoot = path.resolve(input.userDataPath, "runtime")
  if (!inside(path.resolve(input.userDataPath), dataRoot)) throw new RuntimeLayoutError("runtime_data_root_invalid", "Runtime data root must stay within Electron userData")
  return { resourceRoot, dataRoot, pythonPath, manifestPath, entryModule: "scripts.desktop_app", mode }
}
