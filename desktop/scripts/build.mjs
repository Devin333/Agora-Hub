import { cp, mkdir, rm } from "node:fs/promises"
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
await cp(path.join(root, "shared"), path.join(out, "shared"), { recursive: true })
for (const file of ["startup.html", "startup.css", "startup.js"]) {
  await cp(path.join(root, "src", file), path.join(out, file))
}
await cp(path.join(root, "assets", "icon.png"), path.join(out, "icon.png"))

