import { NextRequest, NextResponse } from "next/server";
import { authFailure, authResponse, isSameOrigin, publicAuthPost, readBoundedJson } from "@/lib/auth/portal-server";
export const dynamic = "force-dynamic";
export async function POST(request: NextRequest) {
    if (!isSameOrigin(request))
        return authFailure("auth_invalid_origin", 403);
    const body = await readBoundedJson(request);
    if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).some(key => !["username", "password"].includes(key)) || typeof body.username !== "string" || typeof body.password !== "string" || body.username.length < 3 || body.username.length > 64 || body.password.length < 8 || body.password.length > 256) {
        return authFailure("auth_invalid_request", 400);
    }
    const result = await publicAuthPost("/api/v1/auth/login", { username: body.username, password: body.password }, { signal: AbortSignal.timeout(15000) });
    if (!result.ok && result.errorCode === "auth_invalid_credentials") {
        return NextResponse.json({ success: false, error: { code: result.errorCode, message: "用户名或密码不正确。" } }, { status: 401, headers: { "Cache-Control": "no-store" } });
    }
    return authResponse(result);
}
