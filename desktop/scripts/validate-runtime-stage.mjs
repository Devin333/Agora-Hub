import path from "node:path"
import runtimePackaging from "./runtime-packaging.cjs"

const desktopRoot = path.resolve(import.meta.dirname, "..")
const stage = path.resolve(process.env.AGORA_DESKTOP_RUNTIME_STAGE || path.join(desktopRoot, "runtime-stage"))
try {
  const verified = await runtimePackaging.validateRuntimeStage(stage)
  console.log(`Validated verified desktop runtime envelope: ${stage} (${verified.resourceCount} resources; manifest sha256=${verified.manifestSha256})`)
} catch (error) {
  console.error(`Runtime stage validation failed: ${error.message}`)
  process.exitCode = 2
}
