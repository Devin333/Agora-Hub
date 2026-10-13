import { NextResponse } from "next/server"
import { attachFrontendRuntimeIdentity } from "@/lib/runtime-identity"

export const dynamic = "force-dynamic"

export function GET() {
  try {
    return NextResponse.json({
      success: true,
      data: attachFrontendRuntimeIdentity({
        status: "ok",
        service: "agora-hub-frontend",
        ready: true,
      }),
    }, { headers: { "Cache-Control": "no-store" } })
  } catch {
    return NextResponse.json({
      success: false,
      error: { code: "desktop_runtime_identity_invalid" },
    }, { status: 503, headers: { "Cache-Control": "no-store" } })
  }
}
