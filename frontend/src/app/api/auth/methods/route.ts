import { NextRequest } from "next/server"
import { safeApiGet } from "@/lib/api/server"
import { AUTH_BINDING_COOKIE, authResponse, bindingFor, bindingOptions } from "@/lib/auth/portal-server"

export const dynamic = "force-dynamic"

export async function GET(request: NextRequest) {
  const response = authResponse(await safeApiGet("/api/v1/auth/methods", { signal: AbortSignal.timeout(10000) }))
  response.cookies.set(AUTH_BINDING_COOKIE, bindingFor(request), bindingOptions)
  return response
}
