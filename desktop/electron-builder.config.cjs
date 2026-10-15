const path = require("node:path")
const { createRuntimePackagingHooks } = require("./scripts/runtime-packaging.cjs")
const { createNsisInstallerPackaging } = require("./scripts/nsis-installer-packaging.cjs")

const runtimeStage = path.resolve(process.env.AGORA_DESKTOP_RUNTIME_STAGE || path.join(__dirname, "runtime-stage"))
const runtimeHooks = createRuntimePackagingHooks(runtimeStage)
const installerHooks = createNsisInstallerPackaging({ projectDir: __dirname, runtimeHooks })

module.exports = {
  async beforePack(context) {
    await runtimeHooks.beforePack(context)
    await installerHooks.beforePack(context)
  },
  async afterPack(context) {
    await runtimeHooks.afterPack(context)
    await installerHooks.afterPack(context)
  },
  afterSign: runtimeHooks.afterSign,
  async afterAllArtifactBuild(context) {
    const failures = []
    try { await runtimeHooks.afterAllArtifactBuild(context) } catch (error) { failures.push(error) }
    try { await installerHooks.afterAllArtifactBuild(context) } catch (error) { failures.push(error) }
    if (failures.length) throw new AggregateError(failures, "desktop packaging cleanup failed")
    return []
  },
  appId: "com.agorahub.paperagent",
  productName: "Agora Paper Agent",
  artifactName: "Agora-Paper-Agent-${version}-${arch}.${ext}",
  directories: { output: "dist", buildResources: "assets" },
  files: ["out/**/*", "package.json"],
  extraResources: [],
  asar: true,
  npmRebuild: false,
  win: { target: ["nsis"], icon: "assets/icon.ico" },
  nsis: {
    include: path.join(__dirname, "assets", "nsis", "installer-customization.nsh"),
    oneClick: false,
    perMachine: false,
    allowToChangeInstallationDirectory: true,
    createDesktopShortcut: "always",
    createStartMenuShortcut: true,
    deleteAppDataOnUninstall: false,
    shortcutName: "Agora Paper Agent"
  }
}
