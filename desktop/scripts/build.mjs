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

// The server this build opens until a user saves an address of their own. The
// host validates it again on every launch, so a bad value only means "ask the user".
const serverUrl = process.env.AGORA_DESKTOP_SERVER_URL?.trim() || null
if (serverUrl) {
  let url
  try {
    url = new URL(serverUrl)
  } catch {
    throw new Error("AGORA_DESKTOP_SERVER_URL must be an absolute URL such as https://agora.example.com")
  }
  if (url.protocol !== "https:" || url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("AGORA_DESKTOP_SERVER_URL must be a bare https:// origin without path, query or credentials")
  }
}
await writeFile(
  path.join(out, "server.json"),
  `${JSON.stringify({ schema: "agora.desktop-server/v1", origin: serverUrl ? new URL(serverUrl).origin : null })}\n`,
  "utf8"
)
