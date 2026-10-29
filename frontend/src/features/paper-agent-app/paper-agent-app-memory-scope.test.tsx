import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import type { ComponentProps } from "react"
import type { DesktopCapabilities, DesktopIdentity } from "@/lib/research/desktop/contracts"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { PaperAgentApp } from "./paper-agent-app"

type MemoryProps = ComponentProps<typeof import("./personal-memory-panel").PersonalMemoryPanel>
const controls = vi.hoisted(() => ({
  memoryProps: [] as MemoryProps[],
  lifecycle: { dirty: false, activeTaskCount: 0 },
  owner: "alice",
  fetchDesktopIdentity: vi.fn<(signal?: AbortSignal) => Promise<DesktopIdentity>>(),
  fetchDesktopCapabilities: vi.fn<(signal?: AbortSignal) => Promise<DesktopCapabilities>>(),
  workspace: { owner: "alice", status: "synced" as const, revision: 1, generation: 1, dirty: false, ready: true, message: "", workspace: { visits: [], groups: [], materials: [] } },
}))
vi.mock("@/components/auth/portal-account-provider", () => ({ usePortalAccount: () => ({ resolved: true, session: { user: { userId: controls.owner, username: `${controls.owner}-account` } }, openLogin: vi.fn() }) }))
vi.mock("@/lib/research/use-research-history", () => ({ useOwnedResearchWorkspace: () => controls.workspace }))
vi.mock("@/lib/research/use-guided-research", () => ({ useGuidedResearch: () => ({ active: null, busy: false, saveError: "", start: vi.fn() }) }))
vi.mock("@/lib/research/desktop/client", () => ({
  fetchDesktopIdentity: controls.fetchDesktopIdentity,
  fetchDesktopCapabilities: controls.fetchDesktopCapabilities,
  fetchResearchOperation: vi.fn(),
}))
vi.mock("@/features/portal/components/research-source-entry", () => ({ ResearchSourceEntry: () => null }))
// The quit guard is the App's aggregate of every draft and active request.
vi.mock("./desktop-lifecycle", () => ({ useDesktopLifecycle: (input: { dirty: boolean; activeTaskCount: number }) => { controls.lifecycle = input } }))
vi.mock("./personal-memory-panel", async () => {
  const { useEffect } = await import("react")
  return { PersonalMemoryPanel: (props: MemoryProps) => {
    useEffect(() => { controls.memoryProps.push(props) }, [props])
    return null
  } }
})
vi.mock("./paper-agent-settings-panel", () => ({ PaperAgentSettingsModal: () => null }))
vi.mock("./paper-reader-pane", () => ({ PaperReaderPane: () => null }))

function deferred<T>() {
  let resolve: (value: T) => void = () => { throw new Error("deferred not initialized") }
  const promise = new Promise<T>(finish => { resolve = finish })
  return { promise, resolve }
}

function pendingRuntime(owner: string) {
  const identity = deferred<DesktopIdentity>()
  const capabilities = deferred<DesktopCapabilities>()
  const generation = `gen_${"a".repeat(32)}`
  const value: DesktopIdentity = {
    owner: { userId: owner, memoryNamespace: `research:user:${owner}` },
    generation,
    requestContract: { fingerprint: "sha256-canonical-json-v1", requestId: "stable request identity", notFoundRetry: "query original request" },
  }
  controls.fetchDesktopIdentity.mockReturnValueOnce(identity.promise)
  controls.fetchDesktopCapabilities.mockReturnValueOnce(capabilities.promise)
  return { identity, capabilities, value }
}

async function settleMemoryScope(runtime: ReturnType<typeof pendingRuntime>): Promise<MemoryProps> {
  // Resolve the actual owner runtime request inside async act so its identity
  // commit and the ensuing scope-reset effects finish before callbacks run.
  await act(async () => {
    runtime.identity.resolve(runtime.value)
    runtime.capabilities.resolve({ generation: runtime.value.generation, capabilities: [] })
    await Promise.all([runtime.identity.promise, runtime.capabilities.promise])
  })
  await waitFor(() => {
    expect(screen.getByText(`${runtime.value.owner.userId}-account`)).toBeVisible()
    expect(controls.memoryProps.at(-1)?.namespace).toBe(runtime.value.owner.memoryNamespace)
    expect(controls.lifecycle.dirty).toBe(false)
    expect(controls.lifecycle.activeTaskCount).toBe(0)
  })
  const props = controls.memoryProps.at(-1)
  if (!props || props.namespace !== runtime.value.owner.memoryNamespace) throw new Error("memory scope is not committed")
  return props
}

beforeEach(() => {
  controls.owner = "alice"; controls.workspace.owner = "alice"
  controls.memoryProps.length = 0
  controls.lifecycle = { dirty: false, activeTaskCount: 0 }
  controls.fetchDesktopIdentity.mockReset()
  controls.fetchDesktopCapabilities.mockReset()
})
afterEach(cleanup)

it("ignores every callback from an unmounted previous-owner memory panel", async () => {
  const aliceRuntime = pendingRuntime("alice")
  const view = render(<PaperAgentApp />)
  expect(controls.memoryProps.at(-1)?.namespace).toBeUndefined()
  const previous = await settleMemoryScope(aliceRuntime)
  await act(async () => { previous.onDraftChange(true); previous.onActivityChange(true) })
  expect(controls.lifecycle.dirty).toBe(true)
  expect(controls.lifecycle.activeTaskCount).toBe(1)

  const bobRuntime = pendingRuntime("bob")
  controls.owner = "bob"; controls.workspace.owner = "bob"
  view.rerender(<PaperAgentApp />)
  await settleMemoryScope(bobRuntime)
  await act(async () => {
    previous.onDraftChange(true)
    previous.onActivityChange(true)
    previous.onAuthorizedChange([{ memory_id: `pmem_${"1".repeat(32)}`, content: "old owner authorized content", category: "background", status: "active", revision: 1, source_refs: [{ source_type: "user_input", source_ref: "old-owner-input" }], created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z" }])
    previous.onPolicyChange({ allow_remote_processing: true, revision: 7, created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z" })
  })
  expect(controls.lifecycle.dirty).toBe(false)
  expect(controls.lifecycle.activeTaskCount).toBe(0)
  expect(screen.getByRole("checkbox", { name: "使用已授权记忆" })).toBeDisabled()
  expect(screen.getByRole("checkbox", { name: "使用已授权记忆" })).not.toBeChecked()
})

it("does not let an old panel finally clear the current owner's busy or draft state", async () => {
  const aliceRuntime = pendingRuntime("alice")
  const view = render(<PaperAgentApp />)
  expect(controls.memoryProps.at(-1)?.namespace).toBeUndefined()
  const previous = await settleMemoryScope(aliceRuntime)
  const bobRuntime = pendingRuntime("bob")
  controls.owner = "bob"; controls.workspace.owner = "bob"
  view.rerender(<PaperAgentApp />)
  const next = await settleMemoryScope(bobRuntime)
  await act(async () => { next.onDraftChange(true); next.onActivityChange(true); next.onPolicyChange({ allow_remote_processing: true, revision: 1 }) })
  expect(controls.lifecycle.dirty).toBe(true)
  expect(controls.lifecycle.activeTaskCount).toBe(1)
  await act(async () => { previous.onDraftChange(false); previous.onActivityChange(false); previous.onAuthorizedChange([]); previous.onPolicyChange(null) })
  expect(controls.lifecycle.dirty).toBe(true)
  expect(controls.lifecycle.activeTaskCount).toBe(1)
  expect(screen.getByRole("checkbox", { name: "使用已授权记忆" })).toBeEnabled()
})
