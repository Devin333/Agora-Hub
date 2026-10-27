import { afterEach, describe, expect, it } from "vitest"
import { validateWorkspace } from "./history-model"

const previousProfile = process.env.NEWSROOM_DEPLOYMENT_PROFILE

const legacyWorkspace = {
  visits: [{
    id: "study-1",
    module: "papers" as const,
    question: "Agent",
    href: "/design-demo/papers?question=Agent&researchSession=study-1",
    scrollY: 0,
    title: "Agent",
    groupId: null,
    isFavorite: false,
    createdAt: 1,
    updatedAt: 1,
    deletedAt: null,
  }],
  groups: [],
  materials: [{
    id: "paper-1",
    groupId: null,
    kind: "paper" as const,
    title: "Legacy paper",
    url: "https://example.test/paper",
    readerHref: "/design-demo/papers/2601.01234/read?sourceRevision=revision-1",
    notes: "",
    createdAt: 1,
    updatedAt: 1,
  }],
  reportDrafts: [{
    id: "draft-1",
    groupId: null,
    question: "Agent",
    title: "Legacy draft",
    scope: "",
    notes: "Existing draft remains unchanged",
    materials: [],
    updatedAt: 1,
  }],
}

afterEach(() => {
  if (previousProfile === undefined) delete process.env.NEWSROOM_DEPLOYMENT_PROFILE
  else process.env.NEWSROOM_DEPLOYMENT_PROFILE = previousProfile
})

describe("research history profile migration", () => {
  it("preserves legacy bookmarks in the default profile", () => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "default"
    const validated = validateWorkspace(legacyWorkspace)
    expect(validated?.visits[0].href).toBe(
      "/design-demo/papers?question=Agent&researchSession=study-1",
    )
    expect(validated?.materials?.[0].readerHref).toBe(
      "/design-demo/papers/2601.01234/read?sourceRevision=revision-1",
    )
    expect(validated?.reportDrafts?.[0].notes).toBe("Existing draft remains unchanged")
  })

  it("normalizes the same bookmark idempotently only in the hosted service", () => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "hosted"
    const migrated = validateWorkspace(legacyWorkspace)
    expect(migrated?.visits[0].href).toBe(
      "/papers?question=Agent&researchSession=study-1",
    )
    expect(validateWorkspace(migrated)?.visits[0].href).toBe(migrated?.visits[0].href)
  })
})
