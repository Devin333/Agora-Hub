import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"
import {
  canonicalSemanticJson,
  operationReceiptMatches,
  semanticFingerprint,
} from "@/lib/research/pending-operations"
import {
  validOperationSemantic,
  type ResearchOperationSemantic,
} from "@/lib/research/workspace-items"

type ContractFixture = {
  semantic: ResearchOperationSemantic
  canonicalJson: string
  fingerprint: string
}

const fixturePath = path.resolve(process.cwd(), "../tests/fixtures/research_desktop_request_contracts.json")
const fixtures = (JSON.parse(readFileSync(fixturePath, "utf8")) as {
  cases: ContractFixture[]
}).cases

describe("research operation identity", () => {
  it.each(fixtures)(
    "matches the shared Unicode canonical fixture: $fingerprint",
    async fixture => {
      expect(validOperationSemantic(fixture.semantic)).toBe(true)
      expect(canonicalSemanticJson(fixture.semantic)).toBe(fixture.canonicalJson)
      expect(await semanticFingerprint(fixture.semantic)).toBe(fixture.fingerprint)
    },
  )

  it("changes when a persisted selection or source revision changes", async () => {
    const ask = fixtures.find(item => item.semantic.kind === "ask")?.semantic
    if (!ask || ask.kind !== "ask") throw new Error("ask fixture missing")

    expect(await semanticFingerprint(ask)).not.toBe(
      await semanticFingerprint({ ...ask, selection: { sectionId: "结论" } }),
    )
    expect(await semanticFingerprint(ask)).not.toBe(
      await semanticFingerprint({ ...ask, sourceRevision: "rev-2" }),
    )
  })
  it("rejects a receipt when any durable request identity field differs", () => {
    const expected = {
      requestId: "00000000-0000-4000-8000-000000000000",
      generation: "gen_0123456789abcdef0123456789abcdef",
      kind: "ask" as const,
      fingerprint: `sha256:${"a".repeat(64)}`,
    }
    const receipt = {
      ...expected,
      status: "passed" as const,
      recoverable: true,
      automaticRetryAllowed: false as const,
    }

    expect(operationReceiptMatches(receipt, expected)).toBe(true)
    expect(operationReceiptMatches({ ...receipt, requestId: "other-request" }, expected)).toBe(false)
    expect(operationReceiptMatches({ ...receipt, generation: "gen_ffffffffffffffffffffffffffffffff" }, expected)).toBe(false)
    expect(operationReceiptMatches({ ...receipt, kind: "analyze" }, expected)).toBe(false)
    expect(operationReceiptMatches({ ...receipt, fingerprint: `sha256:${"b".repeat(64)}` }, expected)).toBe(false)
  })
})
