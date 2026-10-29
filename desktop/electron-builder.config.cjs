/**
 * The desktop client is a thin window onto the hosted service: it ships only
 * the Electron host, its preload and the connection page. Build with
 * AGORA_DESKTOP_SERVER_URL set to give the installer a default server address.
 */
module.exports = {
  appId: "com.agorahub.paperagent",
  productName: "Agora Paper Agent",
  artifactName: "Agora-Paper-Agent-${version}-${arch}.${ext}",
  directories: { output: "dist", buildResources: "assets" },
  files: ["out/**/*", "package.json"],
  asar: true,
  npmRebuild: false,
  win: { target: ["nsis"], icon: "assets/icon.ico" },
  nsis: {
    oneClick: false,
    perMachine: false,
    allowToChangeInstallationDirectory: true,
    createDesktopShortcut: "always",
    createStartMenuShortcut: true,
    deleteAppDataOnUninstall: false,
    shortcutName: "Agora Paper Agent"
  }
}
