import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { PaperAgentDataClearPanel } from "./paper-agent-data-clear-panel"
import { DATA_CLEAR_CONFIRMATION_TEXT } from "./paper-agent-data-clear-runtime"

const scope = {
  profilePath: "C:/Users/test/AppData/Roaming/Agora",
  fileCount: 12,
  totalBytes: 4096,
  includes: ["账号与会话", "论文资料、笔记与成果", "个人记忆、配置、缓存和日志"],
  preserves: ["安装资源", "原始导入文件", "外部备份与导出", "其他 profile"],
  credentialPolicy: "只删除当前 profile 的专属系统凭据槽位。",
}

function installBridge(options: {
  expiresAt?: string
  applyResult?: unknown
  applyReject?: boolean
  statusResult?: unknown
} = {}) {
  const preview = vi.fn().mockResolvedValue({
    ok: true,
    state: "CONFIRMATION_REQUIRED",
    code: "data_clear_preview_ready",
    message: "ready",
    previewId: "preview-1",
    expiresAt: options.expiresAt ?? "2099-10-15T08:00:00.000Z",
    scope,
    stopped: true,
  })
  const apply = options.applyReject
    ? vi.fn().mockRejectedValue(new Error("native failed"))
    : vi.fn().mockResolvedValue(options.applyResult ?? { ok: false, state: "CANCELLED", code: "data_clear_cancelled", message: "cancelled", cancelled: true })
  const status = vi.fn().mockResolvedValue(options.statusResult ?? { ok: true, state: "IDLE", code: "data_clear_idle", message: "idle" })
  Object.assign(window, { agoraDesktop: { version: 1, runtime: { clearData: { preview, apply, status } } } })
  return { preview, apply, status }
}

async function previewAndType() {
  await waitFor(() => expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeEnabled())
  fireEvent.click(screen.getByRole("button", { name: "安全停止并预览清除范围" }))
  const input = await screen.findByRole("textbox", { name: "精确确认文本" })
  fireEvent.change(input, { target: { value: DATA_CLEAR_CONFIRMATION_TEXT } })
  return input
}

afterEach(() => {
  cleanup()
  delete (window as unknown as { agoraDesktop?: unknown }).agoraDesktop
  vi.restoreAllMocks()
})

describe("PaperAgentDataClearPanel", () => {
  it("shows a hard unavailable state without a browser clear fallback", () => {
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    expect(screen.getByText("本机数据清除不可用")).toBeInTheDocument()
    expect(screen.getByText(/不会通过浏览器代替本机清除，也不会模拟清除成功/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "安全停止并预览清除范围" })).not.toBeInTheDocument()
  })

  it.each([
    { props: { hasProtectedDrafts: true, activeTaskCount: 0 }, message: /未应用设置或其他未保存草稿/ },
    { props: { hasProtectedDrafts: false, activeTaskCount: 2 }, message: /当前有 2 个活动任务/ },
    { props: { hasProtectedDrafts: false, activeTaskCount: undefined }, message: /无法确认当前活动任务数量/ },
  ])("blocks preview for protected lifecycle state %#", async ({ props, message }) => {
    const bridge = installBridge()
    render(<PaperAgentDataClearPanel {...props} />)
    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeDisabled()
    expect(bridge.preview).not.toHaveBeenCalled()
  })

  it("discloses the real scope, preserves input on native cancel, and never claims deletion", async () => {
    const bridge = installBridge()
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    const input = await previewAndType()

    expect(screen.getByText("账号与会话")).toBeInTheDocument()
    expect(screen.getByText("外部备份与导出")).toBeInTheDocument()
    expect(screen.getByText(scope.credentialPolicy)).toBeInTheDocument()
    expect(screen.getByText(/最小维护回执/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "提交并等待第二次原生确认" }))

    expect(await screen.findByText("原生确认已取消；本次请求未执行删除，输入和预览已保留。")).toBeInTheDocument()
    expect(screen.getByText(/预览已安全停止本机服务；取消后可正常关闭并重新打开 App 恢复服务，不会重放业务请求/)).toBeInTheDocument()
    expect(screen.getByText(/取消不会自动恢复服务/)).toBeInTheDocument()
    expect(input).toHaveValue(DATA_CLEAR_CONFIRMATION_TEXT)
    expect(bridge.apply).toHaveBeenCalledWith({ previewId: "preview-1", confirmationText: DATA_CLEAR_CONFIRMATION_TEXT })
    expect(screen.queryByText("本机数据已清除")).not.toBeInTheDocument()
  })

  it("keeps apply disabled for a typed mismatch and never calls the bridge", async () => {
    const bridge = installBridge()
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    await waitFor(() => expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeEnabled())
    fireEvent.click(screen.getByRole("button", { name: "安全停止并预览清除范围" }))
    const input = await screen.findByRole("textbox", { name: "精确确认文本" })
    fireEvent.change(input, { target: { value: `${DATA_CLEAR_CONFIRMATION_TEXT} ` } })
    expect(screen.getByText(/输入不完全匹配/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "提交并等待第二次原生确认" })).toBeDisabled()
    expect(bridge.apply).not.toHaveBeenCalled()
  })

  it("rejects an expired preview while preserving the exact input", async () => {
    const bridge = installBridge({ expiresAt: "2020-10-15T08:00:00.000Z" })
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    const input = await previewAndType()
    expect(screen.getByText(/此预览已过期/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "提交并等待第二次原生确认" })).toBeDisabled()
    expect(input).toHaveValue(DATA_CLEAR_CONFIRMATION_TEXT)
    expect(bridge.apply).not.toHaveBeenCalled()
  })

  it("preserves preview and input when apply throws or returns an unknown response", async () => {
    const bridge = installBridge({ applyReject: true })
    const { unmount } = render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    const input = await previewAndType()
    fireEvent.click(screen.getByRole("button", { name: "提交并等待第二次原生确认" }))
    expect(await screen.findByText(/结果未确认，可能已经受理/)).toBeInTheDocument()
    expect(screen.queryByText(/取消后可正常关闭并重新打开 App 恢复服务/)).not.toBeInTheDocument()
    expect(input).toHaveValue(DATA_CLEAR_CONFIRMATION_TEXT)
    expect(screen.getByLabelText("本机数据清除预览")).toBeInTheDocument()
    expect(screen.getByText(/此一次性预览不能再次提交/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "提交并等待第二次原生确认" })).toBeDisabled()
    unmount()

    bridge.apply.mockResolvedValueOnce({ ok: true, state: "PREPARED", code: "data_clear_prepared", message: "prepared", operationId: "operation-1", exitRequired: true, token: "must-not-render" })
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    const secondInput = await previewAndType()
    fireEvent.click(screen.getByRole("button", { name: "提交并等待第二次原生确认" }))
    expect(await screen.findByText(/响应无法安全验证/)).toBeInTheDocument()
    expect(secondInput).toHaveValue(DATA_CLEAR_CONFIRMATION_TEXT)
    expect(screen.queryByText(/本机数据保持不变|清除未受理/)).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "提交并等待第二次原生确认" })).toBeDisabled()
    expect(screen.queryByText("must-not-render")).not.toBeInTheDocument()
  })


  it("shows false PREPARED as prepared but incomplete and requires a fresh maintenance status", async () => {
    const bridge = installBridge({ applyResult: { ok: false, state: "PREPARED", code: "desktop_data_clear_host_exit_not_ready", message: "host not ready", operationId: "operation-1", exitRequired: true } })
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    const input = await previewAndType()
    fireEvent.click(screen.getByRole("button", { name: "提交并等待第二次原生确认" }))
    expect(await screen.findByText("已准备清除，尚未完成；宿主退出或清理程序尚未就绪，请先刷新维护状态。")).toBeInTheDocument()
    expect(screen.queryByText(/取消后可正常关闭并重新打开 App 恢复服务/)).not.toBeInTheDocument()
    expect(screen.queryByText(/本机数据保持不变|清除未受理|已经撤销/)).not.toBeInTheDocument()
    expect(input).toHaveValue(DATA_CLEAR_CONFIRMATION_TEXT)
    expect(screen.getByRole("button", { name: "提交并等待第二次原生确认" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "取消本次清除" })).toBeDisabled()
    expect(bridge.apply).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole("button", { name: "刷新清除状态" }))
    await waitFor(() => expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeEnabled())
    expect(screen.getByRole("button", { name: "提交并等待第二次原生确认" })).toBeDisabled()
    fireEvent.click(screen.getByRole("button", { name: "提交并等待第二次原生确认" }))
    expect(bridge.apply).toHaveBeenCalledTimes(1)
  })

  it("reports FAILED_PARTIAL without promising rollback and requires status before a new preview", async () => {
    const bridge = installBridge({ applyResult: { ok: false, state: "FAILED_PARTIAL", code: "desktop_data_clear_failed_partial", message: "partial", operationId: "operation-1" } })
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    const input = await previewAndType()
    fireEvent.click(screen.getByRole("button", { name: "提交并等待第二次原生确认" }))
    expect(await screen.findByText("清除发生部分失败，尚未完成；部分数据可能已删除，请先刷新维护状态。")).toBeInTheDocument()
    expect(screen.queryByText(/取消后可正常关闭并重新打开 App 恢复服务/)).not.toBeInTheDocument()
    expect(screen.queryByText(/本机数据保持不变|清除未受理/)).not.toBeInTheDocument()
    expect(input).toHaveValue(DATA_CLEAR_CONFIRMATION_TEXT)
    expect(screen.getByRole("button", { name: "提交并等待第二次原生确认" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeDisabled()
    bridge.status.mockResolvedValueOnce({ ok: false, state: "FAILED_PARTIAL", code: "desktop_data_clear_failed_partial", message: "partial", operationId: "operation-1" })
    fireEvent.click(screen.getByRole("button", { name: "刷新清除状态" }))
    await waitFor(() => expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeEnabled())
    expect(screen.getByRole("button", { name: "提交并等待第二次原生确认" })).toBeDisabled()
    expect(bridge.preview).toHaveBeenCalledTimes(1)
    expect(bridge.apply).toHaveBeenCalledTimes(1)
  })

  it("does not claim unchanged data when apply prepares maintenance and then throws", async () => {
    const bridge = installBridge()
    let operationPrepared = false
    bridge.apply.mockImplementationOnce(async () => {
      operationPrepared = true
      throw new Error("response lost after prepare")
    })
    bridge.status.mockImplementation(async () => operationPrepared
      ? { ok: false, state: "PREPARED", code: "desktop_data_clear_host_exit_not_ready", message: "host not ready", operationId: "operation-1" }
      : { ok: true, state: "IDLE", code: "data_clear_idle", message: "idle" })
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    const input = await previewAndType()
    fireEvent.click(screen.getByRole("button", { name: "提交并等待第二次原生确认" }))
    expect(await screen.findByText(/结果未确认，可能已经受理/)).toBeInTheDocument()
    expect(screen.queryByText(/取消后可正常关闭并重新打开 App 恢复服务/)).not.toBeInTheDocument()
    expect(operationPrepared).toBe(true)
    expect(screen.queryByText(/本机数据保持不变|清除未受理|当前没有待完成/)).not.toBeInTheDocument()
    expect(input).toHaveValue(DATA_CLEAR_CONFIRMATION_TEXT)
    expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "取消本次清除" })).toBeDisabled()
    fireEvent.click(screen.getByRole("button", { name: "刷新清除状态" }))
    expect(await screen.findByText("已准备清除，尚未完成；请先刷新维护状态。")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "提交并等待第二次原生确认" })).toBeDisabled()
    expect(bridge.apply).toHaveBeenCalledTimes(1)
  })

  it("identifies a confirmed rejection before authorization as no deletion for that request only", async () => {
    installBridge({ applyResult: { ok: false, state: "REJECTED", code: "desktop_data_clear_confirmation_mismatch", message: "mismatch" } })
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    const input = await previewAndType()
    fireEvent.click(screen.getByRole("button", { name: "提交并等待第二次原生确认" }))
    expect(await screen.findByText("清除授权被拒绝；本次请求未执行删除，输入已保留。")).toBeInTheDocument()
    expect(input).toHaveValue(DATA_CLEAR_CONFIRMATION_TEXT)
    expect(screen.getByRole("button", { name: "提交并等待第二次原生确认" })).toBeDisabled()
  })

  it("refuses to offer apply for a successful preview carrying an unknown state", async () => {
    const bridge = installBridge()
    bridge.preview.mockResolvedValueOnce({ ok: true, state: "UNKNOWN", code: "ready", message: "ready", previewId: "preview-1", expiresAt: "2099-10-15T08:00:00.000Z", scope, stopped: true })
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    await waitFor(() => expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeEnabled())
    fireEvent.click(screen.getByRole("button", { name: "安全停止并预览清除范围" }))
    expect(await screen.findByText(/清除预览响应无法安全验证/)).toBeInTheDocument()
    expect(screen.queryByRole("textbox", { name: "精确确认文本" })).not.toBeInTheDocument()
    expect(bridge.apply).not.toHaveBeenCalled()
  })

  it("labels PREPARED only as accepted pending exit cleanup", async () => {
    const bridge = installBridge({ applyResult: { ok: true, state: "PREPARED", code: "data_clear_prepared", message: "prepared", operationId: "operation-1", exitRequired: true } })
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    await previewAndType()
    fireEvent.click(screen.getByRole("button", { name: "提交并等待第二次原生确认" }))
    expect(await screen.findAllByText("已受理、退出后清理，尚未完成。")).not.toHaveLength(0)
    expect(screen.queryByText(/取消后可正常关闭并重新打开 App 恢复服务/)).not.toBeInTheDocument()
    expect(screen.getByText(/只有应用退出后的专用清理程序完成核验，并在维护状态中确认后，才能报告清除完成/)).toBeInTheDocument()
    expect(screen.queryByText("本机数据已清除")).not.toBeInTheDocument()
    const applyButton = screen.getByRole("button", { name: "提交并等待第二次原生确认" })
    expect(applyButton).toBeDisabled()
    fireEvent.click(applyButton)
    expect(bridge.apply).toHaveBeenCalledTimes(1)
  })


  it("does not claim service was stopped when preview cancellation has no successful stop receipt", async () => {
    const bridge = installBridge()
    bridge.preview.mockResolvedValueOnce({ ok: false, state: "CANCELLED", code: "desktop_data_clear_cancelled", message: "cancelled", cancelled: true, stopped: false })
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    await waitFor(() => expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeEnabled())
    fireEvent.click(screen.getByRole("button", { name: "安全停止并预览清除范围" }))
    expect(await screen.findByText("预览已取消；本次预览未执行删除。")).toBeInTheDocument()
    expect(screen.queryByText(/预览已安全停止本机服务/)).not.toBeInTheDocument()
    expect(screen.queryByText(/重新打开 App 恢复服务/)).not.toBeInTheDocument()
    expect(bridge.apply).not.toHaveBeenCalled()
  })

  it("does not offer normal restart on native cancellation while partial maintenance remains fenced", async () => {
    const bridge = installBridge({ statusResult: { ok: false, state: "FAILED_PARTIAL", code: "desktop_data_clear_failed_partial", message: "partial", operationId: "operation-1" } })
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    await previewAndType()
    fireEvent.click(screen.getByRole("button", { name: "提交并等待第二次原生确认" }))
    expect(await screen.findByText("原生确认已取消；本次请求未执行删除，输入和预览已保留。")).toBeInTheDocument()
    expect(screen.getByText(/上次清除发生部分失败/)).toBeInTheDocument()
    expect(screen.queryByText(/重新打开 App 恢复服务/)).not.toBeInTheDocument()
    expect(bridge.apply).toHaveBeenCalledTimes(1)
  })

  it("cancels the renderer confirmation locally without invoking apply", async () => {
    const bridge = installBridge()
    render(<PaperAgentDataClearPanel hasProtectedDrafts={false} activeTaskCount={0} />)
    await previewAndType()
    fireEvent.click(screen.getByRole("button", { name: "取消本次清除" }))
    expect(screen.getByText(/已放弃本次本地确认输入/)).toBeInTheDocument()
    expect(screen.getByText(/预览已安全停止本机服务；取消后可正常关闭并重新打开 App 恢复服务，不会重放业务请求/)).toBeInTheDocument()
    expect(screen.queryByRole("textbox", { name: "精确确认文本" })).not.toBeInTheDocument()
    expect(bridge.apply).not.toHaveBeenCalled()
  })
})

