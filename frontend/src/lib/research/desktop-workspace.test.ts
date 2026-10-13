import { describe, expect, it } from "vitest";
import {
    safeWorkspaceReader,
    validPendingOperation,
    validReaderNotebook,
} from "./workspace-items";

const now = Date.now();
const fingerprint = `sha256:${"a".repeat(64)}`;

function readerNotebook(note: string) {
    return {
        paperId: "2601.01234",
        sourceRevision: "revision-1",
        note,
        question: "question",
        fontSize: 18,
        sectionId: "methods",
        pdfPage: 2,
        updatedAt: now,
    };
}

describe("desktop workspace items", () => {
    it("accepts dotted paper ids and revision-bound reader state", () => {
        expect(
            safeWorkspaceReader("/papers/2601.01234/read?resumePage=2"),
        ).toBe(true);
        expect(validReaderNotebook(readerNotebook("note"))).toBe(true);
    });

    it("uses the server notebook note boundary without silently accepting overflow", () => {
        expect(validReaderNotebook(readerNotebook("n".repeat(20000)))).toBe(true);
        expect(validReaderNotebook(readerNotebook("n".repeat(20001)))).toBe(false);
    });

    it("requires full bounded semantics on pending operations", () => {
        expect(
            validPendingOperation({
                requestId: "00000000-0000-4000-8000-000000000000",
                generation: "gen_0123456789abcdef0123456789abcdef",
                kind: "import",
                fingerprint,
                semantic: {
                    kind: "import",
                    filename: "paper.pdf",
                    sizeBytes: 100,
                    sourceRevision: fingerprint,
                    sourceFingerprint: fingerprint,
                },
                status: "prepared",
                sourceRevision: fingerprint,
                createdAt: now,
                updatedAt: now,
            }),
        ).toBe(true);
        expect(
            validPendingOperation({
                requestId: "request-1",
                generation: "gen_0123456789abcdef0123456789abcdef",
                kind: "import",
                fingerprint,
                status: "prepared",
                createdAt: now,
                updatedAt: now,
            }),
        ).toBe(false);
    });
});
