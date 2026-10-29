/**
 * The desktop client ships only the Electron host, its preload, the
 * connection page and the local stack's compose file; every server runs from
 * published images. Build with AGORA_DESKTOP_OFFICIAL_URL set to offer the
 * official service.
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
  },
  mac: {
    target: [{ target: "dmg", arch: ["arm64"] }],
    icon: "assets/icon.png",
    category: "public.app-category.productivity"
  },
  dmg: { artifactName: "Agora-Paper-Agent-${version}-${arch}.${ext}" }
}
