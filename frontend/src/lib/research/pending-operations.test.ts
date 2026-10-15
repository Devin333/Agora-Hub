import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"
import {
  canonicalSemanticJson,
  mergeResearchOperationUpdate,
  operationReceiptMatches,
  operationReceiptUpdate,
  semanticFingerprint,
} from "@/lib/research/pending-operations"
import {
  validOperationSemantic,
  type ResearchOperationSemantic,
  type ResearchPendingOperation,
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
  it("changes when the exact personal-memory refs or policy revision change", async () => {
    const ask = fixtures.find(item => item.semantic.kind === "ask")?.semantic
    if (!ask || ask.kind !== "ask") throw new Error("ask fixture missing")
    const withMemory = {
      ...ask,
      personalMemory: {
        refs: [{ memoryId: `pmem_${"1".repeat(32)}`, revision: 3 }],
        policyRevision: 2,
      },
    }

    expect(await semanticFingerprint(ask)).not.toBe(await semanticFingerprint(withMemory))
    expect(await semanticFingerprint(withMemory)).not.toBe(await semanticFingerprint({
      ...withMemory,
      personalMemory: { ...withMemory.personalMemory, policyRevision: 3 },
    }))
    expect(await semanticFingerprint(withMemory)).not.toBe(await semanticFingerprint({
      ...withMemory,
      personalMemory: {
        ...withMemory.personalMemory,
        refs: [{ ...withMemory.personalMemory.refs[0], revision: 4 }],
      },
    }))
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

const importRevision = `sha256:${"c".repeat(64)}`
const importPending: ResearchPendingOperation = {
  requestId: "request-import-recovery", generation: `gen_${"a".repeat(32)}`, fingerprint: `sha256:${"b".repeat(64)}`,
  kind: "import", semantic: { kind: "import", filename: "owned.pdf", sizeBytes: 128, sourceRevision: importRevision, sourceFingerprint: importRevision },
  status: "unknown", sourceRevision: importRevision, createdAt: 1, updatedAt: 2,
}
const importReceipt = {
  requestId: importPending.requestId, generation: importPending.generation, fingerprint: importPending.fingerprint,
  kind: "import" as const, status: "passed" as const, importId: "import-confirmed", paperId: "paper-confirmed", sourceRevision: importRevision,
  result: { paperId: "paper-confirmed", sourceRevision: importRevision, sourceFingerprint: importRevision },
  recoverable: false, automaticRetryAllowed: false as const,
}

describe("durable import receipt projection", () => {
  it("retains server-confirmed paper, import and exact source identities", () => {
    expect(operationReceiptUpdate(importReceipt, importPending)).toMatchObject({
      status: "passed", importId: "import-confirmed", paperId: "paper-confirmed", sourceRevision: importRevision, sourceFingerprint: importRevision,
    })
  })
  it("rejects partial, altered or contradictory completed identities", () => {
    expect(operationReceiptUpdate({ ...importReceipt, importId: undefined }, importPending)).toBeNull()
    expect(operationReceiptUpdate({ ...importReceipt, result: { ...importReceipt.result, sourceFingerprint: `sha256:${"d".repeat(64)}` } }, importPending)).toBeNull()
    expect(operationReceiptUpdate({ ...importReceipt, result: { ...importReceipt.result, paperId: "different-paper" } }, importPending)).toBeNull()
    expect(operationReceiptUpdate({ ...importReceipt, paperId: "invalid/paper" }, importPending)).toBeNull()
    expect(operationReceiptUpdate({ ...importReceipt, generation: `gen_${"f".repeat(32)}` }, importPending)).toBeNull()
  })
  it("never promotes a running operation to a completed material", () => {
    expect(operationReceiptUpdate({ ...importReceipt, status: "running" }, importPending)).toEqual({ status: "running" })
  })
})


describe("non-passed source receipt downgrade", () => {
  it("removes an import receipt paper identity while retaining semantic-fixed source identity", () => {
    const completed: ResearchPendingOperation = {
      ...importPending,
      status: "passed",
      paperId: "paper-confirmed",
      importId: "import-confirmed",
      sourceRevision: importRevision,
      sourceFingerprint: importRevision,
    }

    expect(mergeResearchOperationUpdate(completed, { status: "unknown" })).toMatchObject({
      status: "unknown",
      sourceRevision: importRevision,
      sourceFingerprint: importRevision,
    })
    expect(mergeResearchOperationUpdate(completed, { status: "unknown" })).not.toHaveProperty("paperId")
    expect(mergeResearchOperationUpdate(completed, { status: "unknown" })).not.toHaveProperty("importId")
  })

  it("retains the admitted prepare paper while removing stale source output identity", () => {
    const completed: ResearchPendingOperation = {
      requestId: "request-prepare-recovery",
      generation: `gen_${"d".repeat(32)}`,
      fingerprint: `sha256:${"e".repeat(64)}`,
      kind: "prepare",
      semantic: { kind: "prepare", paperId: "paper-known", sourceUrl: "https://doi.org/10.1000/example", sourceType: null },
      status: "passed",
      paperId: "paper-known",
      sourceRevision: importRevision,
      sourceFingerprint: importRevision,
      createdAt: 1,
      updatedAt: 2,
    }
    const downgraded = mergeResearchOperationUpdate(completed, { status: "needs_action" })

    expect(downgraded).toMatchObject({ status: "needs_action", paperId: "paper-known" })
    expect(downgraded).not.toHaveProperty("sourceRevision")
    expect(downgraded).not.toHaveProperty("sourceFingerprint")
  })
})
