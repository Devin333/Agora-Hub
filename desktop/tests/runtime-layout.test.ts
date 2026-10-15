import { mkdir, mkdtemp, writeFile } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { describe, expect, it } from "vitest"
import { resolveRuntimeLayout, RuntimeLayoutError } from "../src/runtime-layout"

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), "agora-desktop-layout-"))
  const appPath = path.join(root, "desktop")
  await mkdir(path.join(root, ".venv", "Scripts"), { recursive: true })
  await mkdir(path.join(root, "scripts"), { recursive: true })
  await mkdir(path.join(root, "frontend"), { recursive: true })
  await mkdir(appPath, { recursive: true })
  await writeFile(path.join(root, "pyproject.toml"), "[project]\nname='test'\n")
  await writeFile(path.join(root, "frontend", "package.json"), "{}")
  await writeFile(path.join(root, ".venv", "Scripts", "python.exe"), "fixture")
  await writeFile(path.join(appPath, "runtime-manifest.dev.json"), JSON.stringify({
    schema: "agora.desktop-runtime-manifest.v1",
    owner: "agora-paper-agent-desktop",
    mode: "development",
    rootRelativePath: "..",
    pythonRelativePath: ".venv/Scripts/python.exe",
    entryModule: "scripts.desktop_app",
    requiredPaths: ["pyproject.toml", "scripts", "frontend/package.json", ".venv/Scripts/python.exe"]
  }))
  return { root, appPath }
}

async function packagedFixture(resources: string[], files: string[] = resources) {
  const root = await mkdtemp(path.join(os.tmpdir(), "agora-packaged-layout-"))
  const resourcesPath = path.join(root, "resources")
  const runtime = path.join(resourcesPath, "runtime")
  await mkdir(runtime, { recursive: true })
  for (const relativePath of new Set(files)) {
    const filePath = path.join(runtime, relativePath)
    await mkdir(path.dirname(filePath), { recursive: true })
    await writeFile(filePath, relativePath)
  }
  await writeFile(path.join(runtime, "desktop-bundle.json"), JSON.stringify({
    schema: "newsroom.desktop-app-bundle/v1",
    bundle_id: "desktop-test",
    runtime: { python: { path: ".venv/Scripts/python.exe" }, node: { path: "node/node.exe" } },
    resources: resources.map(resourcePath => ({ path: resourcePath }))
  }))
  return { root, resourcesPath, runtime }
}

function packagedInput(root: string, resourcesPath: string) {
  return { isPackaged: true, appPath: "unused", resourcesPath, userDataPath: path.join(root, "user-data") }
}

describe("resolveRuntimeLayout", () => {
  it("accepts the explicit owned development root and interpreter", async () => {
    const { root, appPath } = await fixture()
    await expect(resolveRuntimeLayout({ isPackaged: false, appPath, resourcesPath: "unused", userDataPath: path.join(root, "user-data") })).resolves.toMatchObject({
      resourceRoot: root,
      pythonPath: path.join(root, ".venv", "Scripts", "python.exe"),
      dataRoot: path.join(root, "user-data", "runtime")
    })
  })

  it("accepts a normally ordered unique packaged resource inventory", async () => {
    const resources = [
      ".venv/Scripts/python.exe",
      "node/node.exe",
      "scripts/desktop_app/__main__.py",
      "assets/runtime.json"
    ]
    const { root, resourcesPath, runtime } = await packagedFixture(resources)

    await expect(resolveRuntimeLayout(packagedInput(root, resourcesPath))).resolves.toMatchObject({
      resourceRoot: runtime,
      pythonPath: path.join(runtime, ".venv", "Scripts", "python.exe"),
      mode: "packaged"
    })
  })

  it("rejects a duplicate at the start of the packaged resource inventory", async () => {
    const duplicate = ".venv/Scripts/python.exe"
    const { root, resourcesPath } = await packagedFixture([
      duplicate,
      duplicate,
      "node/node.exe",
      "scripts/desktop_app/__main__.py"
    ], [])

    await expect(resolveRuntimeLayout(packagedInput(root, resourcesPath))).rejects.toMatchObject({
      code: "runtime_manifest_invalid",
      message: "Packaged resource inventory contains a duplicate path"
    })
  })

  it("rejects a duplicate at the end of the packaged resource inventory", async () => {
    const duplicate = ".venv/Scripts/python.exe"
    const { root, resourcesPath } = await packagedFixture([
      duplicate,
      "node/node.exe",
      "scripts/desktop_app/__main__.py",
      "assets/runtime.json",
      duplicate
    ], [])

    await expect(resolveRuntimeLayout(packagedInput(root, resourcesPath))).rejects.toMatchObject({
      code: "runtime_manifest_invalid",
      message: "Packaged resource inventory contains a duplicate path"
    })
  })

  it("rejects unsafe paths anywhere in the packaged resource inventory", async () => {
    const { root, resourcesPath } = await packagedFixture([
      ".venv/Scripts/python.exe",
      "node/node.exe",
      "scripts/desktop_app/__main__.py",
      "assets/runtime.json",
      "../outside-runtime.txt"
    ], [])

    await expect(resolveRuntimeLayout(packagedInput(root, resourcesPath))).rejects.toMatchObject({
      code: "runtime_manifest_invalid",
      message: "Packaged resource inventory contains an unsafe path"
    })
  })

  it("still verifies every unique packaged resource against the owned runtime root", async () => {
    const resources = [
      ".venv/Scripts/python.exe",
      "node/node.exe",
      "scripts/desktop_app/__main__.py",
      "assets/missing-last.json"
    ]
    const { root, resourcesPath } = await packagedFixture(resources, resources.slice(0, -1))

    await expect(resolveRuntimeLayout(packagedInput(root, resourcesPath))).rejects.toMatchObject({
      code: "runtime_path_missing"
    })
  })

  it("rejects a late duplicate in an 80k-entry packaged inventory", async () => {
    const duplicate = "inventory/resource-00000.dat"
    const resources = [
      ".venv/Scripts/python.exe",
      "node/node.exe",
      "scripts/desktop_app/__main__.py",
      ...Array.from({ length: 79_997 }, (_, index) => `inventory/resource-${String(index).padStart(5, "0")}.dat`),
      duplicate
    ]
    const { root, resourcesPath } = await packagedFixture(resources, [])

    await expect(resolveRuntimeLayout(packagedInput(root, resourcesPath))).rejects.toMatchObject({
      code: "runtime_manifest_invalid",
      message: "Packaged resource inventory contains a duplicate path"
    })
  })

  it("rejects a manifest that points outside the expected owned root", async () => {
    const { root, appPath } = await fixture()
    const manifestPath = path.join(appPath, "runtime-manifest.dev.json")
    const manifest = JSON.parse(await (await import("node:fs/promises")).readFile(manifestPath, "utf8"))
    manifest.rootRelativePath = "../.."
    await writeFile(manifestPath, JSON.stringify(manifest))
    await expect(resolveRuntimeLayout({ isPackaged: false, appPath, resourcesPath: "unused", userDataPath: path.join(root, "data") })).rejects.toBeInstanceOf(RuntimeLayoutError)
  })
})
