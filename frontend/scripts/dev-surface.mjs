import { spawn } from "node:child_process"
import path from "node:path"

const [, , surface, ...args] = process.argv
const allowedSurfaces = new Set(["portal", "admin"])

if (!allowedSurfaces.has(surface)) {
  console.error("Usage: node scripts/dev-surface.mjs <portal|admin> [next dev args]")
  process.exit(1)
}

const nextBinary = path.join(
  process.cwd(),
  "node_modules",
  ".bin",
  process.platform === "win32" ? "next.cmd" : "next"
)

const child = spawn(nextBinary, ["dev", "--webpack", ...args], {
  env: {
    ...process.env,
    NEWSROOM_FRONTEND_SURFACE: surface
  },
  shell: process.platform === "win32",
  stdio: "inherit"
})

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal)
    return
  }
  process.exit(code ?? 0)
})
