import { NextRequest } from "next/server"
import { desktopGet } from "../_server"

export const dynamic = "force-dynamic"
export const GET = (request: NextRequest) => desktopGet("/api/v1/research/desktop/memory/info", request)
