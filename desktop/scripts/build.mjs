import { cp, mkdir, rm, writeFile } from "node:fs/promises"
import path from "node:path"
import { build } from "esbuild"

const root = path.resolve(import.meta.dirname, "..")
const out = path.join(root, "out")
await rm(out, { recursive: true, force: true })
await mkdir(out, { recursive: true })

const shared = {
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node22",
  external: ["electron"],
  sourcemap: false,
  legalComments: "none",
  logLevel: "info"
}
await build({ ...shared, entryPoints: [path.join(root, "src", "main.ts")], outfile: path.join(out, "main.cjs") })
await build({ ...shared, entryPoints: [path.join(root, "src", "preload.ts")], outfile: path.join(out, "preload.cjs") })
for (const file of ["connect.html", "connect.css", "connect.js"]) {
  await cp(path.join(root, "src", file), path.join(out, file))
}
await cp(path.join(root, "assets", "icon.png"), path.join(out, "icon.png"))

// The local stack's compose file and .env template, read by the host from the
// app bundle and copied to userData/stack before every start.
await mkdir(path.join(out, "local-stack"), { recursive: true })
for (const file of ["compose.images.yml", "env.template"]) {
  await cp(path.join(root, "..", "deploy", "desktop", file), path.join(out, "local-stack", file))
}

// What this build was made for (see src/build-config.ts). The host validates it
// again on every launch, so a bad value only disables the matching feature.
const officialUrl = (process.env.AGORA_DESKTOP_OFFICIAL_URL ?? process.env.AGORA_DESKTOP_SERVER_URL)?.trim() || null
let officialOrigin = null
if (officialUrl) {
  let url
  try {
    url = new URL(officialUrl)
  } catch {
    throw new Error("AGORA_DESKTOP_OFFICIAL_URL must be an absolute URL such as https://agora.example.com")
  }
  if (url.protocol !== "https:" || url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("AGORA_DESKTOP_OFFICIAL_URL must be a bare https:// origin without path, query or credentials")
  }
  officialOrigin = url.origin
}
const imageTag = process.env.AGORA_DESKTOP_IMAGE_TAG?.trim() || null
if (imageTag && !/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(imageTag)) throw new Error("AGORA_DESKTOP_IMAGE_TAG is not a valid image tag")
await writeFile(
  path.join(out, "build.json"),
  `${JSON.stringify({ schema: "agora.desktop-build/v1", officialOrigin, imageTag })}\n`,
  "utf8"
)
