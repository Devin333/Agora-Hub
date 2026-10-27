import { NextRequest } from "next/server";
import { authFailure, authResponse, isSameOrigin, publicAuthPost, readBoundedJson } from "@/lib/auth/portal-server";
import { isHostedProfile } from "@/lib/deployment-profile";
export const dynamic = "force-dynamic";
export async function POST(request: NextRequest) {
    // Hosted accounts are created by an administrator, never from the browser.
    if (isHostedProfile())
        return authFailure("auth_route_unavailable", 404);
    if (!isSameOrigin(request))
        return authFailure("auth_invalid_origin", 403);
    const body = await readBoundedJson(request);
    if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).some(key => !["username", "password"].includes(key)) || typeof body.username !== "string" || typeof body.password !== "string" || body.username.length < 3 || body.username.length > 64 || body.password.length < 8 || body.password.length > 256) {
        return authFailure("auth_invalid_request", 400);
    }
    return authResponse(await publicAuthPost("/api/v1/auth/bootstrap", { username: body.username, password: body.password }, { signal: AbortSignal.timeout(15000) }));
}
