import { NextRequest } from "next/server"
import { forwardHostedApi } from "./_server"

export const dynamic = "force-dynamic"
export const runtime = "nodejs"

type RawRouteContext = { params: Promise<{ path: string[] }> }

export function GET(request: NextRequest, context: RawRouteContext) {
  return forwardHostedApi(request, context)
}

export function HEAD(request: NextRequest, context: RawRouteContext) {
  return forwardHostedApi(request, context)
}

export function POST(request: NextRequest, context: RawRouteContext) {
  return forwardHostedApi(request, context)
}

export function PUT(request: NextRequest, context: RawRouteContext) {
  return forwardHostedApi(request, context)
}

export function PATCH(request: NextRequest, context: RawRouteContext) {
  return forwardHostedApi(request, context)
}

export function DELETE(request: NextRequest, context: RawRouteContext) {
  return forwardHostedApi(request, context)
}
