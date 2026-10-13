import { NextRequest } from "next/server"
import { desktopGet } from "@/lib/research/desktop/server-proxy"
export const dynamic = "force-dynamic"
export const GET = (request: NextRequest) => desktopGet("/api/v1/research/desktop/capabilities", request)
