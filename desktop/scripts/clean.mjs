import { rm } from "node:fs/promises"
import path from "node:path"

const root = path.resolve(import.meta.dirname, "..")
for (const name of ["out", "dist", "test-results", "playwright-report"]) {
  const target = path.resolve(root, name)
  if (path.dirname(target) !== root) throw new Error(`Refusing to clean outside desktop: ${target}`)
  await rm(target, { recursive: true, force: true })
}
