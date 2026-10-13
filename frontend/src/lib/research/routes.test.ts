import { describe, expect, it } from "vitest";
import { migrateLegacyResearchPath, normalizeResearchHref, safeResearchReturnTo } from "@/lib/research/routes";
describe("research desktop routes", () => {
    it("migrates only known research routes and preserves exact location", () => {
        expect(migrateLegacyResearchPath("/design-demo/papers/2401.01234/read?resumeSection=method&resumePage=3#result")).toBe("/papers/2401.01234/read?resumeSection=method&resumePage=3#result");
        expect(migrateLegacyResearchPath("/design-demo/groups/group-1?question=x")).toBe("/research/groups/group-1?question=x");
        expect(migrateLegacyResearchPath("/design-demo/unknown")).toBeNull();
    });
    it("normalizes history idempotently without duplicating query parameters", () => {
        const value = normalizeResearchHref("/design-demo?question=agent&researchSession=one")!;
        expect(value).toBe("/?question=agent&researchSession=one");
        expect(normalizeResearchHref(value)).toBe(value);
    });
    it("normalizes nested legacy returnTo and rejects external returns", () => {
        const value = normalizeResearchHref("/design-demo/papers/2401.01234/read?returnTo=%2Fdesign-demo%2Fpapers%3Fq%3DAgent")!;
        expect(new URL(value, "https://agora.invalid").searchParams.get("returnTo")).toBe("/papers?q=Agent");
        expect(safeResearchReturnTo("https://example.com")).toBe("/papers");
        expect(safeResearchReturnTo("//example.com")).toBe("/papers");
    });
});
