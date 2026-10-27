import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { desktopPost, safeDesktopId } from "@/lib/research/desktop/server-proxy";
vi.mock("next/headers", () => ({ cookies: async () => ({ get: () => ({ value: "session-token" }) }) }));
describe("desktop BFF proxy", () => {
    const originalProfile = process.env.NEWSROOM_DEPLOYMENT_PROFILE;
    const originalOrigin = process.env.NEWSROOM_PUBLIC_ORIGIN;
    const originalInsecure = process.env.NEWSROOM_ALLOW_INSECURE_ORIGIN;
    const originalApi = process.env.NEWSROOM_API_BASE_URL;
    beforeEach(() => {
        process.env.NEWSROOM_DEPLOYMENT_PROFILE = "hosted";
        process.env.NEWSROOM_PUBLIC_ORIGIN = "http://127.0.0.1:4317";
        process.env.NEWSROOM_ALLOW_INSECURE_ORIGIN = "1";
        process.env.NEWSROOM_API_BASE_URL = "http://127.0.0.1:4318";
    });
    afterEach(() => {
        restore("NEWSROOM_DEPLOYMENT_PROFILE", originalProfile);
        restore("NEWSROOM_PUBLIC_ORIGIN", originalOrigin);
        restore("NEWSROOM_ALLOW_INSECURE_ORIGIN", originalInsecure);
        restore("NEWSROOM_API_BASE_URL", originalApi);
        vi.unstubAllGlobals();
    });
    it("accepts real arXiv-style ids", () => {
        expect(safeDesktopId("2401.01234")).toBe(true);
        expect(safeDesktopId("../../escape")).toBe(false);
    });
    it("sends the configured public Origin instead of forwarding browser scope", async () => {
        const backend = vi.fn().mockResolvedValue(new Response(JSON.stringify({ success: true, data: {} }), { headers: { "Content-Type": "application/json" } }));
        vi.stubGlobal("fetch", backend);
        const request = new NextRequest("http://127.0.0.1:4317/api/research/desktop/test", {
            method: "POST",
            headers: { Origin: "http://127.0.0.1:4317", Host: "127.0.0.1:4317", "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json" },
            body: JSON.stringify({ value: "kept", tenantId: "browser-claim" }),
        });
        const response = await desktopPost("/api/v1/research/desktop/test", request, body => ({ value: body.value }));
        expect(response.status).toBe(200);
        const init = backend.mock.calls[0][1] as RequestInit;
        expect(new Headers(init.headers).get("Origin")).toBe("http://127.0.0.1:4317");
        expect(JSON.parse(String(init.body))).toEqual({ value: "kept" });
    });
});
function restore(name: string, value: string | undefined) {
    if (value === undefined)
        delete process.env[name];
    else
        process.env[name] = value;
}
