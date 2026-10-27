import { readFileSync } from "node:fs"
import { resolve } from "node:path"
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { PaperAgentSettingsModal, PaperAgentSettingsPanel } from "./paper-agent-settings-panel"
import type { DesktopConfigView, DesktopRuntimeDiagnostics, DesktopRuntimeStatus } from "./paper-agent-settings-runtime"

const identity = { owner: { userId: "local-user", tenantId: null, memoryNamespace: "research/local-user" }, generation: "gen_11111111111111111111111111111111", requestContract: { fingerprint: "sha256-canonical-json-v1", requestId: "request id", notFoundRetry: "retry with identity" } }
const capabilities = { generation: identity.generation, capabilities: [
  { id: "source", label: "论文来源", required: true, configured: true, reachable: true, verified: true, verifiedAt: "2026-10-14T08:00:00Z", reasonCode: "business_operation_verified", message: "已由受控业务操作验证。", provenance: { verificationKind: "business_operation", operation: "source.search" } },
  { id: "paper-question-answering", label: "论文问答", required: true, configured: true, reachable: false, verified: false, verifiedAt: null, reasonCode: "verification_unknown", message: "已配置，尚无本 generation 的业务验证记录。", provenance: null },
  { id: "rag-question-answering", label: "检索增强问答", required: false, configured: false, reachable: false, verified: false, verifiedAt: null, reasonCode: "component_not_configured", message: "该运行组件尚未配置。", provenance: null },
] }
const info = { storage: "local_sqlite", status: "available", data_root: "F:/private/research", database_path: "F:/private/research/personal-memory.sqlite3", remote_processing_default: false, remote_processing_auto_enabled: false, remote_processing_policy_endpoint: "/api/v1/research/desktop/memory/policy" }
const policy = { allow_remote_processing: false, revision: 4, created_at: "2026-10-14T07:00:00Z", updated_at: "2026-10-14T07:00:00Z" }
const stopped: DesktopRuntimeStatus = { phase: "stopped", state: "STOPPED", code: "runtime_stopped", message: "Stopped", canonicalOrigin: null, instanceKey: null, localTransportReady: false, canRetry: true, checkedAt: "2026-10-14T08:00:00Z" }
const ready: DesktopRuntimeStatus = { ...stopped, phase: "ready", state: "READY", code: "runtime_ready", canonicalOrigin: "http://127.0.0.1:3120", instanceKey: "local-desktop:http://127.0.0.1:3120", localTransportReady: true, canRetry: false, checkedAt: "2026-10-14T08:01:00Z" }
const diagnostics = (runtime: DesktopRuntimeStatus = stopped): DesktopRuntimeDiagnostics => ({ hostVersion: "1.2.3", packaged: true, runtime, recentEvents: [{ at: runtime.checkedAt, command: "status" as const, state: runtime.state, code: runtime.code, message: runtime.message }] })
const response = (data: unknown, status = 200) => new Response(JSON.stringify(status < 400 ? { success: true, data } : { success: false, error: { code: "unavailable", message: "Unavailable" } }), { status, headers: { "Content-Type": "application/json" } })

function installFetch(failedPath?: string) {
  const fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (failedPath && url === failedPath) return response({}, 503)
    if (url === "/api/research/desktop/identity") return response(identity)
    if (url === "/api/research/desktop/capabilities") return response(capabilities)
    if (url === "/api/research/personal-memory/info") return response({ info })
    if (url === "/api/research/personal-memory/policy") return response({ policy })
    return response({}, 404)
  })
  vi.stubGlobal("fetch", fetch)
  return fetch
}
function installDesktop(options: { withConfig?: boolean } = {}) {
  const status = vi.fn<() => Promise<DesktopRuntimeStatus>>().mockResolvedValue(stopped)
  const retry = vi.fn<() => Promise<DesktopRuntimeStatus>>().mockResolvedValue(ready)
  const diagnosticCall = vi.fn<() => Promise<DesktopRuntimeDiagnostics>>().mockImplementation(() => Promise.resolve(diagnostics(retry.mock.calls.length ? ready : stopped)))
  const backup = vi.fn().mockResolvedValue({ ok: true, state: "CREATED", code: "desktop_backup_created", message: "完成", backup: { backupId: "backup-1", fileName: "agora-backup.zip", fileCount: 4, totalBytes: 2048 } })
  const restorePreview = vi.fn().mockResolvedValue({ ok: true, state: "VERIFIED", code: "desktop_restore_verified", message: "完成", previewId: "11111111-1111-4111-8111-111111111111", backupId: "backup-1", isolationVerified: true, personalMemory: { backupId: "backup-1", statusCounts: { active: 2, disabled: 1 }, policyCount: 1, requiresExplicitActivationConfirmation: true, warning: "激活会替换当前个人记忆。" }, expiresAt: "2026-10-14T08:05:00Z" })
  const activate = vi.fn().mockResolvedValue({ ok: true, state: "ACTIVATED", code: "desktop_restore_activated", message: "完成", backupId: "backup-1" })
  const configuration: DesktopConfigView = { schema: "newsroom.desktop-runtime-settings/v1", revision: 2, research: { llm: { routeId: "writer-primary", provider: "openai-compatible", model: "gpt-current", baseUrl: "https://models.example/v1", credentialConfigured: true, credentialBinding: "windows-credential-manager" }, source: { provider: "arxiv", preset: "arxiv-default", apiUrl: "https://export.arxiv.org/api/query", timeoutSeconds: 90 } } }
  let currentConfiguration = configuration
  let previewConfiguration = configuration
  const configShow = vi.fn().mockImplementation(() => Promise.resolve({ ok: true, state: "CONFIGURED", code: "read", message: "read", configuration: currentConfiguration }))
  const configPreview = vi.fn().mockImplementation(candidate => {
    previewConfiguration = { ...currentConfiguration, revision: currentConfiguration.revision + 1, research: { llm: candidate.research.llm ? { ...candidate.research.llm, provider: "openai-compatible", credentialConfigured: true, credentialBinding: "windows-credential-manager" } : null, source: { ...candidate.research.source, provider: "arxiv", preset: "arxiv-default" } } }
    return Promise.resolve({ ok: true, state: "CONFIRMATION_REQUIRED", code: "preview_ready", message: "preview", previewId: "opaque-preview-id", current: currentConfiguration, configuration: previewConfiguration, effectScope: { llmConsumers: ["research"], sourceConsumers: ["research-arxiv-provider"], notChanged: ["guided-paper-search-public-cache"] } })
  })
  const configApply = vi.fn().mockImplementation(() => {
    currentConfiguration = previewConfiguration
    return Promise.resolve({ ok: true, state: "APPLIED", code: "applied", message: "applied", configuration: currentConfiguration, restartRequired: true, runtime: stopped })
  })
  const credentialStatus = vi.fn().mockResolvedValue({ ok: true, code: "configured", configured: true })
  const credentialReplace = vi.fn().mockResolvedValue({ ok: false, code: "cancelled", configured: true, cancelled: true })
  const credentialRemove = vi.fn().mockResolvedValue({ ok: true, code: "removed", configured: false, changed: true })
  const extra = options.withConfig ? { config: { show: configShow, preview: configPreview, apply: configApply }, credentials: { status: credentialStatus, replace: credentialReplace, remove: credentialRemove } } : {}
  Object.assign(window, { agoraDesktop: { version: 1, runtime: { status, retry, diagnostics: diagnosticCall, backup, restore: { preview: restorePreview, activate } }, ...extra } })
  return { status, retry, diagnostics: diagnosticCall, backup, preview: restorePreview, activate, configShow, configPreview, configApply, credentialStatus, credentialReplace, credentialRemove }
}

afterEach(() => { cleanup(); document.body.replaceChildren(); vi.unstubAllGlobals(); vi.restoreAllMocks(); delete (window as unknown as { agoraDesktop?: unknown }).agoraDesktop })

describe("PaperAgentSettingsPanel CSS", () => {
  const css = readFileSync(resolve(process.cwd(), "src/features/paper-agent-app/paper-agent-settings-panel.module.css"), "utf8")

  it("draws every color from the global design tokens instead of literal or legacy shell values", () => {
    expect(css).not.toMatch(/#[0-9a-f]{3,8}\b/i)
    expect(css).not.toMatch(/\brgba?\(/i)
    expect(css).not.toMatch(/\b(?:hsla?|lab|lch|oklab|oklch)\(/i)
    // Legacy paper-agent shell variables are gone; only styles/theme-tokens.css names remain.
    expect(css).not.toMatch(/var\(--(?:text|surface|surface-2|fill|hover|faint|border-strong|radius-[a-z0-9]+|panel-[a-z]+)\)/)
    for (const match of css.matchAll(/color-mix\([^,]*,[^,)]*\)/g)) expect(match[0]).toMatch(/^color-mix\(in srgb, var\(--[a-z-]+\)/)
  })

  it("keeps real status colors for badges, policy notices and the destructive clear flow", () => {
    expect(css).toMatch(/\.badgeSuccess \{[^}]*color: var\(--success-text\)/)
    expect(css).toMatch(/\.badgeWarning \{[^}]*color: var\(--warning-text\)/)
    expect(css).toMatch(/\.badgeDanger \{[^}]*color: var\(--destructive-text\)/)
    expect(css).toMatch(/\.policySafe \{[^}]*var\(--success\)/)
    expect(css).toMatch(/\.inlineError \{[^}]*color: var\(--destructive-text\)/)
    expect(css).toMatch(/\.clearApplyButton \{[^}]*background: var\(--destructive-fill\)/)
  })

  it("lays the dialog out as a 200px navigation column beside a scrolling content column", () => {
    expect(css).toMatch(/\.navigable \{[^}]*grid-template-columns: 200px minmax\(0, 1fr\)/)
    expect(css).toMatch(/\.content \{[^}]*overflow: auto/)
    expect(css).toMatch(/\.navItem\[aria-current="true"\] \{[^}]*background: var\(--accent\)/)
  })
})

describe("PaperAgentSettingsPanel navigation", () => {
  it("lists every section and jumps to the chosen one", async () => {
    installFetch()
    const scrollIntoView = vi.fn()
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scrollIntoView })
    try {
      render(<PaperAgentSettingsPanel />)
      const nav = screen.getByRole("navigation", { name: "设置分区" })
      const items = within(nav).getAllByRole("button")
      expect(items.map(item => item.textContent)).toEqual(["运行概览", "数据位置", "能力与配置", "联网与记忆授权", "宿主诊断与备份", "清除本机数据"])
      expect(items[0]).toHaveAttribute("aria-current", "true")
      for (const id of ["settings-overview", "settings-data", "settings-capabilities", "settings-network", "settings-host", "settings-clear"]) expect(document.getElementById(id)).not.toBeNull()

      fireEvent.click(within(nav).getByRole("button", { name: "联网与记忆授权" }))
      expect(within(nav).getByRole("button", { name: "联网与记忆授权" })).toHaveAttribute("aria-current", "true")
      expect(items[0]).not.toHaveAttribute("aria-current")
      expect(scrollIntoView).toHaveBeenCalledTimes(1)
      expect(scrollIntoView.mock.contexts[0]).toBe(document.getElementById("settings-network"))
    } finally {
      delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
    }
  })
})

describe("PaperAgentSettingsPanel", () => {
  it("shows authoritative storage, capability verification, remote-off policy and native diagnostics", async () => {
    installFetch(); installDesktop(); render(<PaperAgentSettingsPanel />)
    expect(await screen.findByText("F:/private/research/personal-memory.sqlite3")).toBeInTheDocument()
    expect(screen.getByText("research/local-user")).toBeInTheDocument()
    expect(screen.getByText("默认关闭")).toBeInTheDocument()
    expect(screen.getByText("不会自动开启")).toBeInTheDocument()
    expect(screen.getByText("已配置 · 待业务验证")).toBeInTheDocument()
    expect(screen.getByText("待配置")).toBeInTheDocument()
    expect(screen.getByText("个人记忆远程处理未授权")).toBeInTheDocument()
    expect(screen.getAllByText("宿主已停止")).toHaveLength(2)
    expect(screen.getByText("1.2.3")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "创建本机备份" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "选择备份并隔离预览" })).toBeInTheDocument()
    expect(screen.queryByText("sk-secret-value")).not.toBeInTheDocument()
  })

  it("labels browser-only host functions unavailable without hiding authenticated service data", async () => {
    installFetch(); render(<PaperAgentSettingsPanel />)
    expect(await screen.findByText("当前仅在浏览器中运行")).toBeInTheDocument()
    expect(screen.getByText("F:/private/research")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "重试宿主服务" })).not.toBeInTheDocument()
  })

  it("uses native backup and receipt-bound restore operations without synthetic success", async () => {
    installFetch(); const desktop = installDesktop(); render(<PaperAgentSettingsPanel />)
    fireEvent.click(await screen.findByRole("button", { name: "创建本机备份" }))
    await waitFor(() => expect(desktop.backup).toHaveBeenCalledTimes(1))
    expect(await screen.findByText("宿主已确认备份完成。")).toBeInTheDocument()
    expect(screen.getByText(/agora-backup\.zip/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "选择备份并隔离预览" }))
    await waitFor(() => expect(desktop.preview).toHaveBeenCalledTimes(1))
    expect(await screen.findByText("备份已通过隔离恢复预览，尚未激活。")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "激活已验证快照" }))
    await waitFor(() => expect(desktop.activate).toHaveBeenCalledWith({ previewId: "11111111-1111-4111-8111-111111111111" }))
    expect(await screen.findByText("宿主已确认恢复快照激活完成；旧会话按恢复策略失效。")).toBeInTheDocument()
  })

  it("runs the real host retry bridge and keeps service failures explicit", async () => {
    installFetch("/api/research/desktop/capabilities")
    const desktop = installDesktop()
    render(<PaperAgentSettingsPanel />)
    const retry = await screen.findByRole("button", { name: "重试宿主服务" })
    expect(screen.getByText("无法读取能力清单；不会把配置存在误报为可用。")).toBeInTheDocument()
    fireEvent.click(retry)
    await waitFor(() => expect(desktop.retry).toHaveBeenCalledTimes(1))
    expect(await screen.findAllByText("宿主已就绪")).toHaveLength(2)
    expect(screen.getByText("http://127.0.0.1:3120")).toBeInTheDocument()
  })

  it("edits non-secret settings, previews opaque CAS receipt, applies through native confirmation and reports dirty lifecycle", async () => {
    installFetch(); const desktop = installDesktop({ withConfig: true }); const onDraftChange = vi.fn()
    desktop.status.mockResolvedValue(ready)
    desktop.diagnostics.mockResolvedValue(diagnostics(ready))
    render(<PaperAgentSettingsPanel hasOtherDrafts activeTaskCount={0} onDraftChange={onDraftChange} />)
    const model = await screen.findByRole("textbox", { name: "模型" })
    expect(model).toHaveValue("gpt-current")
    const credentialBox = screen.getByText("系统凭据槽位").parentElement?.parentElement
    expect(credentialBox).not.toBeNull()
    expect(within(credentialBox as HTMLElement).getByText(/已配置（未验证）；密钥值不会进入 renderer。/)).toBeInTheDocument()
    expect(screen.queryByLabelText(/密钥|API key/i)).not.toBeInTheDocument()

    fireEvent.change(model, { target: { value: "gpt-next" } })
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(true))
    fireEvent.click(screen.getByRole("button", { name: "校验并预览" }))
    await waitFor(() => expect(desktop.configPreview).toHaveBeenCalledWith({ research: { llm: { routeId: "writer-primary", model: "gpt-next", baseUrl: "https://models.example/v1" }, source: { apiUrl: "https://export.arxiv.org/api/query", timeoutSeconds: 90 } } }))
    expect(await screen.findByText("配置已通过 owner 校验，尚未应用")).toBeInTheDocument()
    expect(screen.getByText(/guided-paper-search-public-cache/)).toBeInTheDocument()
    expect(screen.getByText(/另有未保存草稿/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "在 native 确认后应用" }))
    await waitFor(() => expect(desktop.configApply).toHaveBeenCalledWith({ previewId: "opaque-preview-id" }))
    expect(await screen.findByText(/受控 runtime 已停止/)).toBeInTheDocument()
    expect(screen.getByText("不可用或未验证")).toBeInTheDocument()
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(false))
  })

  it("does not leave a stale ready host badge when post-apply refresh is unavailable", async () => {
    installFetch(); const desktop = installDesktop({ withConfig: true })
    desktop.status.mockResolvedValue(ready)
    desktop.diagnostics.mockResolvedValue(diagnostics(ready))
    desktop.configApply.mockResolvedValueOnce({ ok: true, state: "APPLIED", code: "applied", message: "applied", configuration: { schema: "newsroom.desktop-runtime-settings/v1", revision: 3, research: { llm: { routeId: "writer-primary", provider: "openai-compatible", model: "gpt-unknown-runtime", baseUrl: "https://models.example/v1", credentialConfigured: true, credentialBinding: "windows-credential-manager" }, source: { provider: "arxiv", preset: "arxiv-default", apiUrl: "https://export.arxiv.org/api/query", timeoutSeconds: 90 } } }, restartRequired: true })
    render(<PaperAgentSettingsPanel />)
    const model = await screen.findByRole("textbox", { name: "模型" })
    fireEvent.change(model, { target: { value: "gpt-unknown-runtime" } })
    fireEvent.click(screen.getByRole("button", { name: "校验并预览" }))
    fireEvent.click(await screen.findByRole("button", { name: "在 native 确认后应用" }))

    expect(await screen.findByText("诊断读取失败")).toBeInTheDocument()
    expect(screen.queryByText("宿主已就绪")).not.toBeInTheDocument()
    expect(model).toHaveValue("gpt-unknown-runtime")
  })

  it("keeps settings dirty plus other drafts and inputs on an owner apply error", async () => {
    installFetch(); const desktop = installDesktop({ withConfig: true }); const onDraftChange = vi.fn()
    desktop.configApply.mockRejectedValueOnce(new Error("owner unavailable"))
    render(<PaperAgentSettingsPanel hasOtherDrafts activeTaskCount={0} onDraftChange={onDraftChange} />)
    const model = await screen.findByRole("textbox", { name: "模型" })
    expect(screen.getByText(/另有未保存草稿/)).toBeInTheDocument()
    fireEvent.change(model, { target: { value: "gpt-error-draft" } })
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(true))
    const dirtyCallCount = onDraftChange.mock.calls.length
    fireEvent.click(screen.getByRole("button", { name: "校验并预览" }))
    fireEvent.click(await screen.findByRole("button", { name: "在 native 确认后应用" }))

    expect(await screen.findByText("应用未完成；输入已保留，可重新预览。")).toBeInTheDocument()
    expect(model).toHaveValue("gpt-error-draft")
    expect(screen.getByText("有待应用修改")).toBeInTheDocument()
    expect(screen.getByText(/当前 revision 2/)).toBeInTheDocument()
    expect(screen.getByText(/另有未保存草稿/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "在 native 确认后应用" })).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "校验并预览" })).toBeEnabled()
    expect(onDraftChange).toHaveBeenLastCalledWith(true)
    expect(onDraftChange.mock.calls.slice(dirtyCallCount)).not.toContainEqual([false])
    expect(desktop.configApply).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole("button", { name: "放弃修改" }))
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(false))
    expect(model).toHaveValue("gpt-current")
    expect(screen.getByText(/另有未保存草稿/)).toBeInTheDocument()
  })

  it.each(["ERROR", "CONFLICT"])("retains settings and other draft protection after an owner %s receipt", async state => {
    installFetch(); const desktop = installDesktop({ withConfig: true }); const onDraftChange = vi.fn()
    desktop.configApply.mockResolvedValueOnce({ ok: false, state, code: "desktop_config_owner_rejected", message: "owner rejected apply" })
    render(<PaperAgentSettingsPanel hasOtherDrafts activeTaskCount={0} onDraftChange={onDraftChange} />)
    const model = await screen.findByRole("textbox", { name: "模型" })
    fireEvent.change(model, { target: { value: "gpt-owner-error-draft" } })
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(true))
    const dirtyCallCount = onDraftChange.mock.calls.length
    fireEvent.click(screen.getByRole("button", { name: "校验并预览" }))
    fireEvent.click(await screen.findByRole("button", { name: "在 native 确认后应用" }))

    expect(await screen.findByText("应用未完成；输入已保留，可重新预览。")).toBeInTheDocument()
    expect(model).toHaveValue("gpt-owner-error-draft")
    expect(screen.getByText("有待应用修改")).toBeInTheDocument()
    expect(screen.getByText(/当前 revision 2/)).toBeInTheDocument()
    expect(screen.getByText(/另有未保存草稿/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "在 native 确认后应用" })).not.toBeInTheDocument()
    expect(onDraftChange).toHaveBeenLastCalledWith(true)
    expect(onDraftChange.mock.calls.slice(dirtyCallCount)).not.toContainEqual([false])
  })

  it("keeps settings dirty plus other drafts and inputs on cancelled apply", async () => {
    installFetch(); const desktop = installDesktop({ withConfig: true }); const onDraftChange = vi.fn()
    desktop.configApply.mockResolvedValueOnce({ ok: false, state: "CANCELLED", code: "cancelled", message: "cancelled", cancelled: true })
    render(<PaperAgentSettingsPanel hasOtherDrafts activeTaskCount={0} onDraftChange={onDraftChange} />)
    const model = await screen.findByRole("textbox", { name: "模型" })
    fireEvent.change(model, { target: { value: "gpt-cancelled" } })
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(true))
    fireEvent.click(screen.getByRole("button", { name: "校验并预览" }))
    fireEvent.click(await screen.findByRole("button", { name: "在 native 确认后应用" }))
    expect(await screen.findByText("已取消应用；输入和当前配置均保留。")).toBeInTheDocument()
    expect(model).toHaveValue("gpt-cancelled")
    expect(screen.queryByText("provider 已验证")).not.toBeInTheDocument()
    expect(screen.getByText(/另有未保存草稿/)).toBeInTheDocument()
    expect(onDraftChange).toHaveBeenLastCalledWith(true)
  })

  it("refuses apply when native preview omits the exact Research Arxiv effect scope", async () => {
    installFetch(); const desktop = installDesktop({ withConfig: true })
    desktop.configPreview.mockImplementationOnce(candidate => Promise.resolve({ ok: true, state: "CONFIRMATION_REQUIRED", code: "preview_ready", message: "preview", previewId: "opaque-preview-id", current: { schema: "newsroom.desktop-runtime-settings/v1", revision: 2, research: { llm: { routeId: "writer-primary", provider: "openai-compatible", model: "gpt-current", baseUrl: "https://models.example/v1", credentialConfigured: true, credentialBinding: "windows-credential-manager" }, source: { provider: "arxiv", preset: "arxiv-default", apiUrl: "https://export.arxiv.org/api/query", timeoutSeconds: 90 } } }, configuration: { schema: "newsroom.desktop-runtime-settings/v1", revision: 3, research: { llm: { ...candidate.research.llm, provider: "openai-compatible", credentialConfigured: true, credentialBinding: "windows-credential-manager" }, source: { ...candidate.research.source, provider: "arxiv", preset: "arxiv-default" } } } }))
    render(<PaperAgentSettingsPanel />)
    const model = await screen.findByRole("textbox", { name: "模型" })
    fireEvent.change(model, { target: { value: "gpt-no-scope" } })
    fireEvent.click(screen.getByRole("button", { name: "校验并预览" }))
    expect(await screen.findByText(/未返回可核对的 Research Arxiv 影响范围/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "在 native 确认后应用" })).toBeDisabled()
    expect(desktop.configApply).not.toHaveBeenCalled()
  })

  it("does not clear dirty state when host claims success without authoritative configuration", async () => {
    installFetch(); const desktop = installDesktop({ withConfig: true }); const onDraftChange = vi.fn()
    desktop.configApply.mockResolvedValueOnce({ ok: true, state: "APPLIED", code: "applied", message: "applied" })
    render(<PaperAgentSettingsPanel activeTaskCount={0} onDraftChange={onDraftChange} />)
    const model = await screen.findByRole("textbox", { name: "模型" })
    fireEvent.change(model, { target: { value: "gpt-unknown" } })
    fireEvent.click(screen.getByRole("button", { name: "校验并预览" }))
    fireEvent.click(await screen.findByRole("button", { name: "在 native 确认后应用" }))
    expect(await screen.findByText("应用未完成；输入已保留，可重新预览。")).toBeInTheDocument()
    expect(model).toHaveValue("gpt-unknown")
    expect(onDraftChange).toHaveBeenLastCalledWith(true)
  })

  it("reloads the authoritative saved configuration after the modal closes and reopens", async () => {
    installFetch(); const desktop = installDesktop({ withConfig: true })
    const close = vi.fn()
    const view = render(<PaperAgentSettingsModal open onRequestClose={close} />)
    const model = await screen.findByRole("textbox", { name: "模型" })
    fireEvent.change(model, { target: { value: "gpt-reopened" } })
    fireEvent.click(screen.getByRole("button", { name: "校验并预览" }))
    fireEvent.click(await screen.findByRole("button", { name: "在 native 确认后应用" }))
    await screen.findByText(/受控 runtime 已停止/)

    view.rerender(<PaperAgentSettingsModal open={false} onRequestClose={close} />)
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
    view.rerender(<PaperAgentSettingsModal open onRequestClose={close} />)

    expect(await screen.findByRole("textbox", { name: "模型" })).toHaveValue("gpt-reopened")
    expect(desktop.configShow).toHaveBeenCalledTimes(2)
  })

  it("does not offer editable config in a browser without the typed bridge", async () => {
    installFetch(); render(<PaperAgentSettingsPanel />)
    expect(await screen.findByText("浏览器没有运行配置能力")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "校验并预览" })).not.toBeInTheDocument()
  })

})



describe("PaperAgentSettingsModal", () => {
  it("contains forward and reverse Tab focus, skips disabled controls, and contains programmatic focus", async () => {
    installFetch()
    installDesktop({ withConfig: true })
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

    const enabled = Array.from(dialog.querySelectorAll<HTMLElement>("button, input, select, textarea, a[href], summary, [tabindex]"))
      .filter(element => element.tabIndex >= 0 && !element.matches(":disabled"))
    expect(enabled.length).toBeGreaterThan(2)
    expect(enabled).not.toContain(screen.getByRole("button", { name: "校验并预览" }))

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
    installFetch()
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
    installFetch()
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
    installFetch()
    const desktop = installDesktop({ withConfig: true })
    let resolveConfig!: (value: Awaited<ReturnType<typeof desktop.configShow>>) => void
    desktop.configShow.mockImplementationOnce(() => new Promise(resolve => { resolveConfig = resolve }))
    const outside = document.createElement("button")
    document.body.append(outside)
    render(<PaperAgentSettingsModal open onRequestClose={vi.fn()} />)
    const dialog = screen.getByRole("dialog")
    outside.focus()
    expect(dialog).toContainElement(document.activeElement as HTMLElement)

    resolveConfig({ ok: true, state: "LOADED", code: "loaded", message: "loaded", configuration: { schema: "newsroom.desktop-runtime-settings/v1", revision: 2, research: { llm: { routeId: "writer-primary", provider: "openai-compatible", model: "gpt-current", baseUrl: "https://models.example/v1", credentialConfigured: true, credentialBinding: "windows-credential-manager" }, source: { provider: "arxiv", preset: "arxiv-default", apiUrl: "https://export.arxiv.org/api/query", timeoutSeconds: 90 } } } })
    const model = await screen.findByRole("textbox", { name: "模型" })
    model.focus()
    fireEvent.keyDown(document, { key: "Tab" })
    expect(dialog).toContainElement(document.activeElement as HTMLElement)
    expect(document.activeElement).not.toBe(outside)
    outside.remove()
  })
})

describe("PaperAgentSettingsPanel data clear lifecycle integration", () => {
  function addClearBridge() {
    const clearData = {
      status: vi.fn().mockResolvedValue({ ok: true, state: "IDLE", code: "data_clear_idle", message: "idle" }),
      preview: vi.fn().mockResolvedValue({ ok: true, state: "CONFIRMATION_REQUIRED", code: "data_clear_preview_ready", message: "ready", previewId: "preview-1", expiresAt: "2099-10-15T08:00:00.000Z", stopped: true, scope: { profilePath: "C:/Users/test/AppData/Roaming/Agora", fileCount: 5, totalBytes: 100, includes: ["账号与会话"], preserves: ["外部备份与导出"], credentialPolicy: "只删除当前 profile 的专属凭据槽位。" } }),
      apply: vi.fn(),
    }
    const desktop = (window as unknown as { agoraDesktop: { runtime: Record<string, unknown> } }).agoraDesktop
    desktop.runtime.clearData = clearData
    return clearData
  }

  it("does not treat the one-time phrase as a business draft", async () => {
    installFetch(); installDesktop({ withConfig: true }); addClearBridge(); const onDraftChange = vi.fn()
    render(<PaperAgentSettingsPanel activeTaskCount={0} onDraftChange={onDraftChange} />)
    await screen.findByRole("textbox", { name: "模型" })
    await waitFor(() => expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeEnabled())
    fireEvent.click(screen.getByRole("button", { name: "安全停止并预览清除范围" }))
    const clearInput = await screen.findByRole("textbox", { name: "精确确认文本" })
    fireEvent.change(clearInput, { target: { value: "删除本机全部 Agora 数据" } })
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(false))
  })

  it("keeps an existing config draft and blocks destructive preview", async () => {
    installFetch(); installDesktop({ withConfig: true }); const clearData = addClearBridge(); const onDraftChange = vi.fn()
    render(<PaperAgentSettingsPanel activeTaskCount={0} onDraftChange={onDraftChange} />)
    const model = await screen.findByRole("textbox", { name: "模型" })
    fireEvent.change(model, { target: { value: "gpt-unsaved-before-clear" } })
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(true))
    expect(screen.getByText(/存在未应用设置或其他未保存草稿/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeDisabled()
    expect(model).toHaveValue("gpt-unsaved-before-clear")
    expect(clearData.preview).not.toHaveBeenCalled()
  })
})

