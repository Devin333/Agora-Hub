import { NextResponse } from "next/server"
import { resolveDeploymentProfile } from "@/lib/deployment-profile"

export const dynamic = "force-dynamic"

export function GET() {
  return NextResponse.json({
    success: true,
    data: {
      status: "ok",
      service: "agora-hub-frontend",
      deploymentProfile: resolveDeploymentProfile(),
      ready: true,
    },
  }, { headers: { "Cache-Control": "no-store" } })
}
