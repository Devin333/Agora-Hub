import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { modelConnectionsChangedEvent } from "@/lib/account/model-connection-events"
import type { AuthSession } from "@/lib/papers/types"
import { PaperAgentSettingsModal, PaperAgentSettingsPanel } from "./paper-agent-settings-panel"

const account = vi.hoisted(() => ({
  session: null as AuthSession | null,
  pending: false,
  signOut: vi.fn(async () => undefined),
}))
vi.mock("@/components/auth/portal-account-provider", () => ({
  usePortalAccount: () => ({ resolved: true, error: null, openLogin: vi.fn(), ...account }),
}))

const admin: AuthSession = { user: { userId: "user-admin", username: "alice", role: "admin", createdAt: "2026-10-01T08:00:00Z" }, sessionId: "session-1", expiresAt: "2026-12-31T00:00:00Z" }
const member: AuthSession = { user: { userId: "user-member", username: "bob", role: "user" }, sessionId: "session-2", expiresAt: "2026-12-31T00:00:00Z" }

const capabilities = { generation: `gen_${"1".repeat(32)}`, capabilities: [
  { id: "source", label: "论文来源", required: true, configured: true, reachable: true, verified: true, verifiedAt: "2026-10-14T08:00:00Z", reasonCode: "business_operation_verified", message: "已由受控业务操作验证。", provenance: { verificationKind: "business_operation", operation: "source.search" } },
  { id: "paper-question-answering", label: "论文问答", required: true, configured: false, reachable: false, verified: false, verifiedAt: null, reasonCode: "model_connection_required", message: "请先在设置里为“回答”配置可用的模型连接。", provenance: null },
] }
const policy = { allow_remote_processing: false, revision: 4, created_at: "2026-10-14T07:00:00Z", updated_at: "2026-10-14T07:00:00Z" }
const reasoning = { parameter: "deepseek_thinking", efforts: ["off", "low", "high", "max"], default: "high" }
// The same shape the API's account_view returns (interfaces/services/model_connections/views.py).
const catalog = { providers: [
  { id: "deepseek", label: "DeepSeek 官方", kind: "catalog", protocol: "openai_chat", baseUrl: "https://api.deepseek.com", defaultModel: "deepseek-flash", models: [
    { id: "deepseek-flash", label: "DeepSeek V4.1 Flash", contextWindowTokens: 1_000_000, maxOutputTokens: 384_000, reasoning, structuredOutput: true },
    { id: "deepseek-small", label: "DeepSeek Small", contextWindowTokens: 16_000, maxOutputTokens: null, reasoning: { parameter: "none", efforts: [], default: null }, structuredOutput: false },
  ] },
  { id: "custom-openai-compatible", label: "自定义（OpenAI 兼容）", kind: "custom", protocol: "openai_chat", reasoningParameters: ["none", "openai_reasoning_effort"], reasoningEfforts: { none: [], openai_reasoning_effort: ["off", "low", "medium", "high"] }, structuredOutputModes: ["json_object", "none"] },
] }
const purposes = [
  { id: "answer", label: "回答", requirement: { inputTokens: 342_639, outputTokens: 900, structuredOutput: false } },
  { id: "evidence_summary", label: "证据摘要", requirement: { inputTokens: 41_500, outputTokens: 3_000, structuredOutput: false } },
  { id: "section_navigation", label: "章节导航", requirement: { inputTokens: 40_906, outputTokens: 1_500, structuredOutput: false } },
  { id: "candidate_generation", label: "候选生成", requirement: { inputTokens: 64_000, outputTokens: 2_048, structuredOutput: true } },
]
// What POST /preview answers per draft model (interfaces/services/model_connections/views.py).
const previewFor = (model: string) => model === "deepseek-small"
  ? {
    contextWindowTokens: 16_000, maxOutputTokens: null, effectiveReasoningEffort: null, structuredOutput: "none",
    fits: {
      answer: { fits: false, requiredWindow: 343_539, reason: "model_connection_window_too_small" },
      evidence_summary: { fits: false, requiredWindow: 44_500, reason: "model_connection_window_too_small" },
      section_navigation: { fits: false, requiredWindow: 42_406, reason: "model_connection_window_too_small" },
      candidate_generation: { fits: false, requiredWindow: null, reason: "model_connection_structured_output_required" },
    },
  }
  : {
    contextWindowTokens: 1_000_000, maxOutputTokens: 384_000, effectiveReasoningEffort: "high", structuredOutput: "json_object",
    fits: Object.fromEntries(["answer", "evidence_summary", "section_navigation", "candidate_generation"].map(id => [id, { fits: true, requiredWindow: 400_000, reason: null }])),
  }
const allFit = Object.fromEntries(purposes.map(purpose => [purpose.id, { fits: true, requiredWindow: 400_000, reason: null }]))
const flash = {
  id: "mc-0123456789abcdef", label: "DeepSeek 官方 · DeepSeek V4.1 Flash", providerId: "deepseek", providerLabel: "DeepSeek 官方", custom: false,
  protocol: "openai_chat", baseUrl: "https://api.deepseek.com", model: "deepseek-flash", modelLabel: "DeepSeek V4.1 Flash", reasoningEffort: null,
  reasoningParameter: "deepseek_thinking", contextWindowTokens: 1_000_000, maxOutputTokens: 384_000, structuredOutput: "json_object",
  status: "unverified", credentialConfigured: true, keyHint: "a1b2", revision: 1, createdAt: "2026-10-20T08:00:00Z",
  updatedAt: "2026-10-20T08:00:00Z", verifiedAt: null, purposes: [], fits: allFit,
}
type View = { revision: number; connections: Array<Record<string, unknown>>; assignments: Record<string, string | null> }
const viewOf = (state: View) => ({ ...state, purposes, catalog })
const emptyView: View = { revision: 0, connections: [], assignments: { answer: null, evidence_summary: null, section_navigation: null, candidate_generation: null } }
const withFlash = (overrides: Record<string, unknown> = {}): View => ({ revision: 1, connections: [{ ...flash, ...overrides }], assignments: emptyView.assignments })
const accounts = [
  { userId: "user-admin", username: "alice", role: "admin", enabled: true, createdAt: "2026-10-01T08:00:00Z" },
  { userId: "user-member", username: "bob", role: "user", enabled: true, createdAt: "2026-10-02T08:00:00Z" },
]

const ok = (data: unknown) => new Response(JSON.stringify({ success: true, data }), { status: 200, headers: { "Content-Type": "application/json" } })
const fail = (code: string, status: number) => new Response(JSON.stringify({ success: false, error: { code, message: "upstream detail" } }), { status, headers: { "Content-Type": "application/json" } })

type Handler = (init: RequestInit | undefined) => Response | Promise<Response>
type Server = { view: View; routes: Map<string, Handler>; fetch: ReturnType<typeof vi.fn> }

/** A fake BFF: each "METHOD path" answers with the account service envelope. */
function installServer(options: { view?: View } = {}): Server {
  const server: Server = { view: options.view ?? emptyView, routes: new Map(), fetch: vi.fn() }
  const routes = server.routes
  routes.set("GET /api/research/desktop/capabilities", () => ok(capabilities))
  routes.set("GET /api/research/personal-memory/policy", () => ok({ policy }))
  routes.set("GET /api/v1/account/model-connections", () => ok(viewOf(server.view)))
  routes.set("POST /api/v1/account/model-connections/preview", init => ok(previewFor(String(JSON.parse(String(init?.body)).model))))
  routes.set("GET /api/v1/admin/accounts", () => ok({ accounts }))
  server.fetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${String(input)}`
    const handler = routes.get(key)
    return handler ? handler(init) : fail("not_found", 404)
  })
  vi.stubGlobal("fetch", server.fetch)
  return server
}

function bodyOf(server: Server, key: string): unknown {
  const call = server.fetch.mock.calls.find(([input, init]) => `${(init as RequestInit | undefined)?.method ?? "GET"} ${String(input)}` === key)
  return call ? JSON.parse(String((call[1] as RequestInit).body)) : undefined
}

function calledWith(server: Server, key: string): boolean {
  return server.fetch.mock.calls.some(([input, init]) => `${(init as RequestInit | undefined)?.method ?? "GET"} ${String(input)}` === key)
}

beforeEach(() => {
  account.session = member
  account.pending = false
  account.signOut.mockClear()
})

afterEach(() => { cleanup(); document.body.replaceChildren(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe("PaperAgentSettingsPanel CSS", () => {
  const css = readFileSync(resolve(process.cwd(), "src/features/paper-agent-app/paper-agent-settings-panel.module.css"), "utf8")

  it("draws every color from the global design tokens instead of literal or legacy shell values", () => {
    expect(css).not.toMatch(/#[0-9a-f]{3,8}\b/i)
    expect(css).not.toMatch(/\brgba?\(/i)
    expect(css).not.toMatch(/\b(?:hsla?|lab|lch|oklab|oklch)\(/i)
    expect(css).not.toMatch(/var\(--(?:text|surface|surface-2|fill|hover|faint|border-strong|radius-[a-z0-9]+|panel-[a-z]+)\)/)
    for (const match of css.matchAll(/color-mix\([^,]*,[^,)]*\)/g)) expect(match[0]).toMatch(/^color-mix\(in srgb, var\(--[a-z-]+\)/)
  })

  it("fills recessed wells with the ink-alpha secondary fill, since --card equals --dialog in dark", () => {
    expect(css).toMatch(/\.panel \{[^}]*--settings-well: var\(--secondary\)/)
    expect(css).not.toMatch(/background: var\(--(?:card|muted)\)/)
    for (const selector of ["summaryCard", "notice", "dataTableHeader"]) {
      expect(css).toMatch(new RegExp(String.raw`\.${selector} \{[^}]*background: var\(--settings-well\)`))
    }
  })

  it("keeps real status colors for badges, policy notices and errors", () => {
    expect(css).toMatch(/\.badgeSuccess \{[^}]*color: var\(--success-text\)/)
    expect(css).toMatch(/\.badgeWarning \{[^}]*color: var\(--warning-text\)/)
    expect(css).toMatch(/\.badgeDanger \{[^}]*color: var\(--destructive-text\)/)
    expect(css).toMatch(/\.policySafe \{[^}]*var\(--success\)/)
    expect(css).toMatch(/\.inlineError \{[^}]*color: var\(--destructive-text\)/)
  })

  it("lays the dialog out as a 200px navigation column beside a scrolling content column", () => {
    expect(css).toMatch(/\.navigable \{[^}]*grid-template-columns: 200px minmax\(0, 1fr\)/)
    expect(css).toMatch(/\.content \{[^}]*overflow: auto/)
    expect(css).toMatch(/\.navItem\[aria-current="true"\] \{[^}]*background: var\(--accent\)/)
  })

  it("defines every class the settings panel and its sections use", () => {
    const sources = [
      "src/features/paper-agent-app/paper-agent-settings-panel.tsx",
      "src/features/paper-agent-app/settings/account-admin-section.tsx",
      "src/features/paper-agent-app/settings/account-password-section.tsx",
      "src/features/paper-agent-app/settings/model-connections-section.tsx",
      "src/features/paper-agent-app/settings/model-connection-form.tsx",
      "src/features/paper-agent-app/settings/settings-ui.tsx",
    ].map(path => readFileSync(resolve(process.cwd(), path), "utf8")).join("\n")
    const used = new Set(Array.from(sources.matchAll(/styles\.([A-Za-z]+)/g), match => match[1]))
    for (const name of used) expect(css, name).toMatch(new RegExp(String.raw`\.${name}\b`))
  })
})

describe("PaperAgentSettingsPanel navigation", () => {
  function withScroll(run: (scrollIntoView: ReturnType<typeof vi.fn>) => Promise<void>) {
    const scrollIntoView = vi.fn()
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scrollIntoView })
    return run(scrollIntoView).finally(() => { delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView })
  }

  it("lists the member sections and jumps to the chosen one", () => withScroll(async scrollIntoView => {
    installServer()
    render(<PaperAgentSettingsPanel />)
    const nav = screen.getByRole("navigation", { name: "设置分区" })
    const items = within(nav).getAllByRole("button")
    expect(items.map(item => item.textContent)).toEqual(["账号与密码", "模型连接", "能力状态", "联网与记忆授权"])
    expect(items[0]).toHaveAttribute("aria-current", "true")
    expect(document.getElementById("settings-admin")).toBeNull()

    fireEvent.click(within(nav).getByRole("button", { name: "联网与记忆授权" }))
    expect(within(nav).getByRole("button", { name: "联网与记忆授权" })).toHaveAttribute("aria-current", "true")
    expect(items[0]).not.toHaveAttribute("aria-current")
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(document.getElementById("settings-network"))
  }))

  it("adds account management for administrators only", async () => {
    account.session = admin
    const server = installServer()
    render(<PaperAgentSettingsPanel />)
    const nav = screen.getByRole("navigation", { name: "设置分区" })
    expect(within(nav).getAllByRole("button").map(item => item.textContent)).toContain("账号管理")
    expect(await screen.findByText("alice（你）")).toBeInTheDocument()
    expect(calledWith(server, "GET /api/v1/admin/accounts")).toBe(true)
  })

  it("never asks a member's browser for the account list", async () => {
    const server = installServer()
    render(<PaperAgentSettingsPanel />)
    await screen.findByText("还没有模型连接")
    expect(calledWith(server, "GET /api/v1/admin/accounts")).toBe(false)
  })

  it("opens at the section a model connection failure asked for", () => withScroll(async scrollIntoView => {
    installServer()
    render(<PaperAgentSettingsPanel initialSection="model" />)
    expect(within(screen.getByRole("navigation", { name: "设置分区" })).getByRole("button", { name: "模型连接" })).toHaveAttribute("aria-current", "true")
    expect(scrollIntoView.mock.contexts[0]).toBe(document.getElementById("settings-model"))
  }))
})

describe("PaperAgentSettingsPanel overview", () => {
  it("shows the signed-in account, real capability states and the remote-off memory policy", async () => {
    account.session = admin
    installServer()
    render(<PaperAgentSettingsPanel />)

    expect(screen.getByText("alice · 管理员")).toBeInTheDocument()
    const capabilityList = await screen.findByText("论文来源")
    const list = capabilityList.closest("ul") as HTMLElement
    expect(within(list).getByText("已验证")).toBeInTheDocument()
    expect(within(list).getByText("待配置")).toBeInTheDocument()
    expect(await screen.findByText("个人记忆远程处理未授权")).toBeInTheDocument()
    expect(screen.getByText("未授权（关闭）")).toBeInTheDocument()
    // The summary reflects the stored connections, not an assumption.
    expect(screen.getAllByText("未配置").length).toBeGreaterThan(0)
    expect(screen.queryByText(/F:\//)).not.toBeInTheDocument()
  })

  it("reports read failures explicitly instead of claiming availability", async () => {
    const server = installServer()
    server.routes.set("GET /api/research/desktop/capabilities", () => fail("unavailable", 503))
    server.routes.set("GET /api/research/personal-memory/policy", () => fail("unavailable", 503))
    render(<PaperAgentSettingsPanel />)

    expect(await screen.findByText("无法读取能力清单；不会把配置存在误报为可用。")).toBeInTheDocument()
    expect(screen.getByText("无法确认个人记忆远程处理策略；按未授权处理。")).toBeInTheDocument()
    expect(screen.getByText("策略读取失败")).toBeInTheDocument()
  })

  it("signs out through the account provider", async () => {
    installServer()
    render(<PaperAgentSettingsPanel />)
    fireEvent.click(screen.getByRole("button", { name: "退出登录" }))
    expect(account.signOut).toHaveBeenCalledOnce()
  })
})

describe("PaperAgentSettingsPanel model connections", () => {
  async function openNewForm() {
    fireEvent.click(await screen.findByRole("button", { name: "添加模型连接" }))
    return screen.getByLabelText("API Key")
  }

  it("requires the key on first save, sends it once and never shows it again", async () => {
    const server = installServer()
    server.routes.set("POST /api/v1/account/model-connections", () => {
      server.view = withFlash()
      return ok(viewOf(server.view))
    })
    const changed = vi.fn()
    window.addEventListener(modelConnectionsChangedEvent, changed)
    const onDraftChange = vi.fn()
    try {
      render(<PaperAgentSettingsPanel onDraftChange={onDraftChange} />)
      const key = await openNewForm()
      expect(screen.getByLabelText("服务商")).toHaveValue("deepseek")
      expect(screen.getByLabelText("模型")).toHaveValue("deepseek-flash")
      expect(screen.getByText("https://api.deepseek.com")).toBeInTheDocument()

      fireEvent.change(screen.getByLabelText("思考强度"), { target: { value: "max" } })
      await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(true))
      fireEvent.click(screen.getByRole("button", { name: "保存" }))
      expect(await screen.findByText("请填写 API Key。")).toBeInTheDocument()
      expect(calledWith(server, "POST /api/v1/account/model-connections")).toBe(false)

      fireEvent.change(key, { target: { value: "  sk-test-secret-value  " } })
      fireEvent.click(screen.getByRole("button", { name: "保存" }))

      expect(await screen.findByText("模型连接已保存。建议点击“验证”确认可用。")).toBeInTheDocument()
      expect(bodyOf(server, "POST /api/v1/account/model-connections")).toEqual({
        providerId: "deepseek", model: "deepseek-flash", reasoningEffort: "max", apiKey: "sk-test-secret-value", expectedRevision: 0,
      })
      expect(screen.queryByLabelText("API Key")).not.toBeInTheDocument()
      expect(document.body.textContent).not.toContain("sk-test-secret-value")
      expect(screen.getByText(/Key 尾号 a1b2/)).toBeInTheDocument()
      expect(screen.getAllByText("已保存 · 未验证").length).toBeGreaterThan(0)
      expect(changed).toHaveBeenCalledOnce()
      await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(false))
    } finally {
      window.removeEventListener(modelConnectionsChangedEvent, changed)
    }
  })

  it("asks the API which purposes a draft fits, without ever sending the key", async () => {
    const server = installServer()
    render(<PaperAgentSettingsPanel />)
    const key = await openNewForm()
    const preview = screen.getByRole("list", { name: "可用于哪些用途" })
    expect(await within(preview).findAllByText("可用")).toHaveLength(4)
    expect(bodyOf(server, "POST /api/v1/account/model-connections/preview")).toEqual({ providerId: "deepseek", model: "deepseek-flash" })

    fireEvent.change(key, { target: { value: "sk-typed-into-the-form" } })
    fireEvent.change(screen.getByLabelText("模型"), { target: { value: "deepseek-small" } })
    expect(await within(preview).findAllByText("不可用")).toHaveLength(4)
    expect(within(preview).getByText("需要能返回 JSON 对象的模型。")).toBeInTheDocument()
    expect(within(preview).getByText("需要至少 34.4 万 token 窗口，这个模型只有 1.6 万 token。")).toBeInTheDocument()
    const previewBodies = server.fetch.mock.calls
      .filter(([input, init]) => String(input).endsWith("/preview") && (init as RequestInit).method === "POST")
      .map(([, init]) => String((init as RequestInit).body))
    expect(previewBodies.length).toBeGreaterThan(0)
    for (const body of previewBodies) expect(body).not.toContain("sk-typed-into-the-form")
  })

  it("waits for a custom draft's address and window before asking", async () => {
    const server = installServer()
    render(<PaperAgentSettingsPanel />)
    await openNewForm()
    fireEvent.change(screen.getByLabelText("服务商"), { target: { value: "custom-openai-compatible" } })
    fireEvent.change(screen.getByLabelText("模型 ID"), { target: { value: "qwen3-max" } })
    await new Promise(resolve => setTimeout(resolve, 400))
    const customPreviews = () => server.fetch.mock.calls.filter(([input, init]) =>
      String(input).endsWith("/preview") && String((init as RequestInit).body).includes("custom-openai-compatible"))
    expect(customPreviews()).toHaveLength(0)
    expect(screen.getAllByText("填好模型信息后自动判断。")).toHaveLength(4)

    fireEvent.change(screen.getByLabelText("服务地址（Base URL）"), { target: { value: "https://gateway.example.com/v1" } })
    fireEvent.change(screen.getByLabelText("上下文窗口（token）"), { target: { value: "128000" } })
    await waitFor(() => expect(customPreviews()).toHaveLength(1))
    expect(JSON.parse(String((customPreviews()[0][1] as RequestInit).body))).toEqual({
      providerId: "custom-openai-compatible", model: "qwen3-max", baseUrl: "https://gateway.example.com/v1",
      contextWindowTokens: 128000, reasoningParameter: "none", structuredOutput: "json_object",
    })
  })

  it("asks a custom provider for its address, window and reasoning style", async () => {
    const server = installServer()
    server.routes.set("POST /api/v1/account/model-connections/discover", () => ok({ models: ["qwen3-coder", "qwen3-max"] }))
    server.routes.set("POST /api/v1/account/model-connections", () => fail("model_endpoint_not_allowed", 400))
    render(<PaperAgentSettingsPanel />)
    const key = await openNewForm()
    fireEvent.change(screen.getByLabelText("服务商"), { target: { value: "custom-openai-compatible" } })
    fireEvent.change(screen.getByLabelText("服务地址（Base URL）"), { target: { value: "https://gateway.example.com/v1" } })
    fireEvent.change(key, { target: { value: "sk-gateway-key" } })
    fireEvent.click(screen.getByRole("button", { name: "获取模型列表" }))
    await waitFor(() => expect(bodyOf(server, "POST /api/v1/account/model-connections/discover")).toEqual({
      providerId: "custom-openai-compatible", baseUrl: "https://gateway.example.com/v1", apiKey: "sk-gateway-key",
    }))

    fireEvent.change(screen.getByLabelText("模型 ID"), { target: { value: "qwen3-max" } })
    fireEvent.click(screen.getByRole("button", { name: "保存" }))
    expect(await screen.findByText("请填写上下文窗口大小（正整数）。")).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText("上下文窗口（token）"), { target: { value: "128,000" } })
    fireEvent.change(screen.getByLabelText("思考参数格式"), { target: { value: "openai_reasoning_effort" } })
    fireEvent.change(screen.getByLabelText("思考强度"), { target: { value: "low" } })
    fireEvent.click(screen.getByRole("button", { name: "保存" }))

    expect(await screen.findByText("这个模型地址不被允许：只能用公网 HTTPS 地址，内网地址需要管理员加入白名单。")).toBeInTheDocument()
    expect(bodyOf(server, "POST /api/v1/account/model-connections")).toEqual({
      providerId: "custom-openai-compatible", model: "qwen3-max", reasoningEffort: "low", apiKey: "sk-gateway-key",
      baseUrl: "https://gateway.example.com/v1", contextWindowTokens: 128000, reasoningParameter: "openai_reasoning_effort",
      structuredOutput: "json_object", expectedRevision: 0,
    })
    // A refused save keeps the form and its key so the user can fix the address.
    expect(screen.getByLabelText("API Key")).toHaveValue("sk-gateway-key")
  })

  it("edits a stored connection without resending its key", async () => {
    const server = installServer({ view: withFlash() })
    server.routes.set(`PATCH /api/v1/account/model-connections/${flash.id}`, () => ok(viewOf(withFlash({ reasoningEffort: "low", revision: 2 }))))
    render(<PaperAgentSettingsPanel />)
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }))
    expect(screen.getByLabelText("API Key")).toHaveAttribute("placeholder", "已保存（尾号 a1b2），留空则保持不变")
    fireEvent.change(screen.getByLabelText("思考强度"), { target: { value: "low" } })
    fireEvent.click(screen.getByRole("button", { name: "保存" }))

    expect(await screen.findByText("模型连接已更新。")).toBeInTheDocument()
    expect(bodyOf(server, `PATCH /api/v1/account/model-connections/${flash.id}`)).toEqual({
      providerId: "deepseek", model: "deepseek-flash", label: flash.label, reasoningEffort: "low", expectedRevision: 1,
    })
  })

  it("asks for the key again when the provider changes", async () => {
    installServer({ view: withFlash() })
    render(<PaperAgentSettingsPanel />)
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }))
    fireEvent.change(screen.getByLabelText("服务商"), { target: { value: "custom-openai-compatible" } })
    expect(screen.getByLabelText("API Key")).toHaveAttribute("placeholder", "粘贴你的 API Key")
  })

  it("assigns a purpose and offers only connections whose window holds it", async () => {
    const small = {
      ...flash, id: "mc-fedcba9876543210", label: "小模型", model: "deepseek-small", modelLabel: "DeepSeek Small", contextWindowTokens: 16_000,
      fits: { ...allFit, answer: { fits: false, requiredWindow: 343_539, reason: "model_connection_window_too_small" } },
    }
    const server = installServer({ view: { ...withFlash(), connections: [flash, small] } })
    server.routes.set("PUT /api/v1/account/model-connections/assignments", () => ok(viewOf({ ...withFlash(), revision: 2, assignments: { ...emptyView.assignments, answer: flash.id } })))
    render(<PaperAgentSettingsPanel />)
    const answer = await screen.findByLabelText("回答")
    expect(within(answer).getByRole("option", { name: "小模型（需要 34.4 万 token 窗口）" })).toBeDisabled()

    fireEvent.change(answer, { target: { value: flash.id } })

    expect(await screen.findByText("用途分配已保存。")).toBeInTheDocument()
    expect(bodyOf(server, "PUT /api/v1/account/model-connections/assignments")).toEqual({ assignments: { answer: flash.id }, expectedRevision: 1 })
  })

  it("explains a window too small for a purpose with the numbers", async () => {
    const server = installServer({ view: withFlash() })
    server.routes.set("PUT /api/v1/account/model-connections/assignments", () => new Response(JSON.stringify({
      success: false,
      error: {
        code: "model_connection_window_too_small",
        message: "upstream",
        details: { purpose: "answer", purposeLabel: "回答", requiredWindow: 343_539, contextWindow: 128_000 },
      },
    }), { status: 400, headers: { "Content-Type": "application/json" } }))
    render(<PaperAgentSettingsPanel />)
    fireEvent.change(await screen.findByLabelText("回答"), { target: { value: flash.id } })
    expect(await screen.findByText("“回答”需要至少 34.4 万 token 的上下文窗口，这个模型只有 12.8 万 token。请换一个窗口更大的模型。")).toBeInTheDocument()
  })

  it("explains a key that must be entered again and blocks verification", async () => {
    installServer({ view: withFlash({ status: "key_rotated", credentialConfigured: false, keyHint: null }) })
    render(<PaperAgentSettingsPanel />)
    expect(await screen.findByText("服务器的加密密钥已更换，原来保存的 Key 无法再解开，请重新填写一次。")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "验证" })).toBeDisabled()
    fireEvent.click(screen.getByRole("button", { name: "重新填写" }))
    expect(screen.getByLabelText("API Key")).toHaveAttribute("placeholder", "粘贴你的 API Key")
  })

  it("verifies through the provider and shows its rejection as a fixable error", async () => {
    const server = installServer({ view: withFlash() })
    server.routes.set(`POST /api/v1/account/model-connections/${flash.id}/verify`, () => fail("model_connection_rejected", 400))
    render(<PaperAgentSettingsPanel />)
    fireEvent.click(await screen.findByRole("button", { name: "验证" }))
    expect(await screen.findByText("模型服务拒绝了这个 API Key，请检查后重试。")).toBeInTheDocument()
    expect(screen.queryByText("upstream detail")).not.toBeInTheDocument()

    server.routes.set(`POST /api/v1/account/model-connections/${flash.id}/verify`, () => ok(viewOf(withFlash({ status: "verified", verifiedAt: "2026-10-27T08:00:00Z" }))))
    fireEvent.click(screen.getByRole("button", { name: "验证" }))
    expect(await screen.findByText("验证通过，模型服务可以正常使用这个 Key。")).toBeInTheDocument()
    expect(screen.getAllByText("已验证可用").length).toBeGreaterThan(0)
  })

  it("refreshes to the stored state when another tab changed the settings first", async () => {
    const server = installServer({ view: withFlash() })
    server.routes.set(`PATCH /api/v1/account/model-connections/${flash.id}`, () => {
      server.view = withFlash({ keyHint: "z9y8", revision: 3 })
      return fail("model_connection_conflict", 409)
    })
    render(<PaperAgentSettingsPanel />)
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }))
    fireEvent.change(screen.getByLabelText("思考强度"), { target: { value: "low" } })
    fireEvent.click(screen.getByRole("button", { name: "保存" }))

    expect(await screen.findByText("模型设置已在其他窗口被修改，已为你刷新，请确认后再保存。")).toBeInTheDocument()
    expect(await screen.findByText(/Key 尾号 z9y8/)).toBeInTheDocument()
  })

  it("deletes a connection only after confirmation", async () => {
    // The API fills both from one document: the assignment and the connection's purposes agree.
    const server = installServer({ view: { ...withFlash({ purposes: ["answer"] }), assignments: { ...emptyView.assignments, answer: flash.id } } })
    const deletion = `DELETE /api/v1/account/model-connections/${flash.id}?expectedRevision=1`
    server.routes.set(deletion, () => ok(viewOf({ ...emptyView, revision: 2 })))
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true)
    render(<PaperAgentSettingsPanel />)
    fireEvent.click(await screen.findByRole("button", { name: "删除" }))
    expect(confirm.mock.calls[0][0]).toContain("“回答”将没有可用的模型")
    expect(calledWith(server, deletion)).toBe(false)
    fireEvent.click(screen.getByRole("button", { name: "删除" }))
    expect(await screen.findByText("模型连接已删除。")).toBeInTheDocument()
    expect(confirm).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument()
  })
})

describe("PaperAgentSettingsPanel password", () => {
  function fill(current: string, next: string, confirm = next) {
    fireEvent.change(screen.getByLabelText("当前密码"), { target: { value: current } })
    fireEvent.change(screen.getByLabelText("新密码（至少 8 位）"), { target: { value: next } })
    fireEvent.change(screen.getByLabelText("再次输入新密码"), { target: { value: confirm } })
  }

  it("checks the new password locally before calling the server", async () => {
    const server = installServer()
    render(<PaperAgentSettingsPanel />)
    fill("old-password", "short")
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }))
    expect(screen.getByText("新密码至少需要 8 位。")).toBeInTheDocument()
    fill("old-password", "new-password-1", "new-password-2")
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }))
    expect(screen.getByText("两次输入的新密码不一致。")).toBeInTheDocument()
    fill("same-password", "same-password")
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }))
    expect(screen.getByText("新密码不能与当前密码相同。")).toBeInTheDocument()
    expect(calledWith(server, "POST /api/auth/password")).toBe(false)
  })

  it("changes the password through the cookie-rotating auth route and clears the form", async () => {
    const server = installServer()
    server.routes.set("POST /api/auth/password", () => ok({ session: { user: member.user } }))
    const onDraftChange = vi.fn()
    render(<PaperAgentSettingsPanel onDraftChange={onDraftChange} />)
    fill("old-password", "new-password-1")
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(true))
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }))

    expect(await screen.findByText("密码已修改。其他设备上的登录已失效，需要用新密码重新登录。")).toBeInTheDocument()
    expect(bodyOf(server, "POST /api/auth/password")).toEqual({ currentPassword: "old-password", newPassword: "new-password-1" })
    expect(screen.getByLabelText("当前密码")).toHaveValue("")
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(false))
  })

  it("keeps the typed input when the current password is wrong", async () => {
    const server = installServer()
    server.routes.set("POST /api/auth/password", () => fail("auth_invalid_credentials", 401))
    render(<PaperAgentSettingsPanel />)
    fill("wrong-password", "new-password-1")
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }))
    expect(await screen.findByText("当前密码不正确。")).toBeInTheDocument()
    expect(screen.getByLabelText("新密码（至少 8 位）")).toHaveValue("new-password-1")
  })
})

describe("PaperAgentSettingsPanel account administration", () => {
  beforeEach(() => { account.session = admin })

  it("creates an account and shows its initial password exactly once", async () => {
    const server = installServer()
    server.routes.set("POST /api/v1/admin/accounts", () => ok({ account: { userId: "user-carol", username: "carol", role: "user", enabled: true, createdAt: "2026-10-27T08:00:00Z" }, initialPassword: "issued-initial-password" }))
    const onDraftChange = vi.fn()
    render(<PaperAgentSettingsPanel onDraftChange={onDraftChange} />)
    await screen.findByText("alice（你）")

    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "ca" } })
    fireEvent.click(screen.getByRole("button", { name: "创建并生成初始密码" }))
    expect(screen.getByText("用户名需要 3–64 个字符。")).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: " carol " } })
    fireEvent.click(screen.getByRole("button", { name: "创建并生成初始密码" }))

    expect(await screen.findByText("issued-initial-password")).toBeInTheDocument()
    expect(bodyOf(server, "POST /api/v1/admin/accounts")).toEqual({ username: "carol", role: "user" })
    expect(screen.getByText("共 3 个，其中 3 个启用中")).toBeInTheDocument()
    // An unread one-time password protects the dialog from closing silently.
    expect(onDraftChange).toHaveBeenLastCalledWith(true)
    fireEvent.click(screen.getByRole("button", { name: "我已记下" }))
    expect(screen.queryByText("issued-initial-password")).not.toBeInTheDocument()
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(false))
  })

  it("disables another account after confirmation and never offers disabling yourself", async () => {
    const server = installServer()
    server.routes.set("POST /api/v1/admin/accounts/user-member/disable", () => ok({ account: { ...accounts[1], enabled: false, disabledAt: "2026-10-27T08:00:00Z" } }))
    vi.spyOn(window, "confirm").mockReturnValue(true)
    render(<PaperAgentSettingsPanel />)
    const self = (await screen.findByText("alice（你）")).closest("li") as HTMLElement
    expect(within(self).getByRole("button", { name: "停用" })).toBeDisabled()

    const other = screen.getByText("bob").closest("li") as HTMLElement
    fireEvent.click(within(other).getByRole("button", { name: "停用" }))
    expect(await screen.findByText("已停用「bob」。")).toBeInTheDocument()
    expect(within(screen.getByText("bob").closest("li") as HTMLElement).getByText("已停用")).toBeInTheDocument()
  })

  it("resets a password and reports the last-admin rule from the server", async () => {
    const server = installServer()
    server.routes.set("POST /api/v1/admin/accounts/user-member/password", () => ok({ account: accounts[1], initialPassword: "reset-password-value" }))
    server.routes.set("PUT /api/v1/admin/accounts/user-admin/role", () => fail("last_admin_required", 409))
    vi.spyOn(window, "confirm").mockReturnValue(true)
    render(<PaperAgentSettingsPanel />)
    const other = (await screen.findByText("bob")).closest("li") as HTMLElement
    fireEvent.click(within(other).getByRole("button", { name: "重置密码" }))
    expect(await screen.findByText("reset-password-value")).toBeInTheDocument()
    expect(screen.getByText("「bob」的新密码")).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText("角色：alice"), { target: { value: "user" } })
    expect(await screen.findByText("至少需要保留一个启用中的管理员。")).toBeInTheDocument()
    expect(screen.getByLabelText("角色：alice")).toHaveValue("admin")
  })
})

describe("PaperAgentSettingsPanel draft aggregation", () => {
  it("reports one dirty flag while any section still holds input", async () => {
    installServer()
    const onDraftChange = vi.fn()
    render(<PaperAgentSettingsPanel onDraftChange={onDraftChange} />)
    fireEvent.click(await screen.findByRole("button", { name: "添加模型连接" }))
    fireEvent.change(screen.getByLabelText("当前密码"), { target: { value: "typed" } })
    fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "sk-typed" } })
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(true))
    fireEvent.change(screen.getByLabelText("当前密码"), { target: { value: "" } })
    expect(onDraftChange).toHaveBeenLastCalledWith(true)
    fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "" } })
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(false))
    expect(onDraftChange.mock.calls.map(([dirty]) => dirty)).toEqual([true, false])
  })
})

describe("PaperAgentSettingsModal", () => {
  it("contains forward and reverse Tab focus, skips disabled controls, and contains programmatic focus", async () => {
    installServer()
    const outside = document.createElement("button")
    outside.textContent = "outside"
    document.body.append(outside)
    outside.focus()

    const { unmount } = render(<PaperAgentSettingsModal open onRequestClose={vi.fn()} />)
    const dialog = screen.getByRole("dialog")
    const close = screen.getByRole("button", { name: "关闭设置面板" })
    await waitFor(() => expect(close).toHaveFocus())
    expect(outside).toHaveAttribute("inert")
    expect(outside).toHaveAttribute("aria-hidden", "true")
    await screen.findByRole("button", { name: "添加模型连接" })

    const enabled = Array.from(dialog.querySelectorAll<HTMLElement>("button, input, select, textarea, a[href], summary, [tabindex]"))
      .filter(element => element.tabIndex >= 0 && !element.matches(":disabled"))
    expect(enabled.length).toBeGreaterThan(2)
    expect(enabled).not.toContain(screen.getByRole("button", { name: "修改密码" }))

    enabled[0].focus()
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true })
    expect(document.activeElement).toBe(enabled.at(-1))
    fireEvent.keyDown(document, { key: "Tab" })
    expect(document.activeElement).toBe(enabled[0])

    outside.focus()
    expect(dialog).toContainElement(document.activeElement as HTMLElement)
    unmount()
    expect(outside).toHaveFocus()
    outside.remove()
  })

  it("restores pre-existing background inert and aria-hidden values", async () => {
    installServer()
    const background = document.createElement("div")
    background.setAttribute("inert", "legacy")
    background.setAttribute("aria-hidden", "false")
    document.body.append(background)
    const { unmount } = render(<PaperAgentSettingsModal open onRequestClose={vi.fn()} />)
    await screen.findByRole("dialog")
    expect(background).toHaveAttribute("inert", "")
    expect(background).toHaveAttribute("aria-hidden", "true")
    unmount()
    expect(background).toHaveAttribute("inert", "legacy")
    expect(background).toHaveAttribute("aria-hidden", "false")
    background.remove()
  })

  it("routes Escape, overlay, and close button through one close callback each", async () => {
    installServer()
    const onRequestClose = vi.fn()
    const { container } = render(<PaperAgentSettingsModal open overlayClassName="settings-overlay" onRequestClose={onRequestClose} />)
    await screen.findByRole("dialog")

    fireEvent.keyDown(document, { key: "Escape" })
    expect(onRequestClose).toHaveBeenCalledTimes(1)
    fireEvent.mouseDown(container.querySelector(".settings-overlay") as HTMLElement)
    expect(onRequestClose).toHaveBeenCalledTimes(2)
    fireEvent.click(screen.getByRole("button", { name: "关闭设置面板" }))
    expect(onRequestClose).toHaveBeenCalledTimes(3)
  })

  it("keeps focus contained when asynchronous controls are inserted", async () => {
    const server = installServer()
    let release!: () => void
    const gate = new Promise<void>(resolve => { release = resolve })
    server.routes.set("GET /api/v1/account/model-connections", async () => { await gate; return ok(viewOf(emptyView)) })
    const outside = document.createElement("button")
    document.body.append(outside)
    render(<PaperAgentSettingsModal open onRequestClose={vi.fn()} />)
    const dialog = screen.getByRole("dialog")
    outside.focus()
    expect(dialog).toContainElement(document.activeElement as HTMLElement)

    release()
    const add = await screen.findByRole("button", { name: "添加模型连接" })
    add.focus()
    fireEvent.keyDown(document, { key: "Tab" })
    expect(dialog).toContainElement(document.activeElement as HTMLElement)
    expect(document.activeElement).not.toBe(outside)
    outside.remove()
  })
})
