import { NextRequest } from "next/server"
import { proxyAuthPost } from "@/lib/auth/portal-server"

export const dynamic = "force-dynamic"
export async function POST(request: NextRequest) {
  return proxyAuthPost(request, "/api/v1/auth/otp/challenges", (body) =>
    ["phone", "email"].includes(String(body.channel)) && typeof body.destination === "string" && body.destination.length <= 320 && body.consent === true
      ? { channel: body.channel, destination: body.destination, consent: true } : null)
}
