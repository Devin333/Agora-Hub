const CLEAR_PHRASE = "删除本机全部 Agora 数据"
const title = document.getElementById("title")
const message = document.getElementById("message")
const status = document.getElementById("status")
const retry = document.getElementById("retry")
const details = document.getElementById("details")
const diagnostics = document.getElementById("diagnostics")
const clearPanel = document.getElementById("clear-panel")
const clearExplanation = document.getElementById("clear-explanation")
const clearPreview = document.getElementById("clear-preview")
const clearConfirmation = document.getElementById("clear-confirmation")
const clearApply = document.getElementById("clear-apply")
const clearRefresh = document.getElementById("clear-refresh")
let currentPreviewId = null
let refreshTimer = null

function render(view) {
  status.textContent = `${view.state} · ${view.code}`
  if (view.phase === "ready") {
    title.textContent = "论文工作台已就绪"
    message.textContent = "正在打开受信本机页面。"
  } else if (view.phase === "starting") {
    title.textContent = "正在启动论文工作台"
    message.textContent = view.message
  } else if (view.phase === "stopped") {
    title.textContent = "本机服务尚未启动"
    message.textContent = view.message
  } else {
    title.textContent = "论文工作台暂时不可用"
    message.textContent = `${view.message} 未知状态不会被视为启动成功。`
  }
  retry.hidden = !view.canRetry
}

function showClearStatus(view) {
  clearPanel.hidden = false
  retry.hidden = true
  title.textContent = view.state === "FAILED_PARTIAL" ? "上次数据清除未完整完成" : "本机数据清除正在阻止正常启动"
  message.textContent = view.message
  status.textContent = `${view.state} · ${view.code}`
  clearExplanation.textContent = view.state === "FAILED_PARTIAL"
    ? "旧 profile 会原样保持阻塞。请选择重新预览剩余 owned 数据，再逐字确认；不会自动删除或创建空资料根。"
    : "旧 profile 会保持原位且不会导航到旧 frontend。可检查状态，或显式重新预览；owner 若不允许该状态重试会如实拒绝。"
  clearPreview.textContent = view.state === "FAILED_PARTIAL" ? "重新预览剩余清理" : "重新预览清理状态"
}

async function readClearStatus() {
  const bridge = window.agoraDesktop?.runtime?.clearData
  if (!bridge) return null
  try { return await bridge.status() } catch { return { ok: false, state: "UNKNOWN", code: "desktop_clear_bridge_unavailable", message: "本机数据清除 bridge 不可用。" } }
}

async function refresh() {
  if (refreshTimer !== null) {
    clearTimeout(refreshTimer)
    refreshTimer = null
  }
  const clearState = await readClearStatus()
  if (clearState && (!clearState.ok || ["PREPARED", "FINALIZING", "FAILED_PARTIAL"].includes(clearState.state))) {
    showClearStatus(clearState)
    return
  }
  clearPanel.hidden = true
  let view
  try { view = await window.agoraDesktop.runtime.status() }
  catch { view = { phase: "unknown", state: "UNKNOWN", code: "desktop_bridge_unavailable", message: "安全宿主桥接不可用。", canRetry: true } }
  render(view)
  if (view.phase === "starting") refreshTimer = setTimeout(() => void refresh(), 2000)
}

retry.addEventListener("click", async () => {
  retry.disabled = true
  title.textContent = "正在重试"
  try { render(await window.agoraDesktop.runtime.retry()) }
  finally { retry.disabled = false }
})

details.addEventListener("click", async () => {
  diagnostics.hidden = !diagnostics.hidden
  if (!diagnostics.hidden) diagnostics.textContent = JSON.stringify(await window.agoraDesktop.runtime.diagnostics(), null, 2)
})

clearRefresh.addEventListener("click", async () => {
  const view = await readClearStatus()
  if (view) showClearStatus(view)
})

clearPreview.addEventListener("click", async () => {
  clearPreview.disabled = true
  clearApply.disabled = true
  clearConfirmation.value = ""
  currentPreviewId = null
  clearExplanation.textContent = "正在由 owner 核对并停止/确认已停止本 App runtime。实测 drain 可能超过一分钟；此步骤只预览，不删除。"
  try {
    const preview = await window.agoraDesktop.runtime.clearData.preview()
    status.textContent = `${preview.state} · ${preview.code}`
    if (!preview.ok || !preview.previewId || !preview.scope) {
      clearExplanation.textContent = preview.message
      return
    }
    currentPreviewId = preview.previewId
    clearExplanation.textContent = [
      preview.message,
      `目标：${preview.scope.profilePath}`,
      `文件：${preview.scope.fileCount}；总字节：${preview.scope.totalBytes}`,
      `保留：${preview.scope.preserves.join("；")}`,
      `请输入：${CLEAR_PHRASE}`
    ].join("\n")
    clearConfirmation.focus()
  } finally {
    clearPreview.disabled = false
  }
})

clearConfirmation.addEventListener("input", () => {
  clearApply.disabled = !currentPreviewId || clearConfirmation.value !== CLEAR_PHRASE
})

clearApply.addEventListener("click", async () => {
  if (!currentPreviewId || clearConfirmation.value !== CLEAR_PHRASE) return
  clearApply.disabled = true
  const result = await window.agoraDesktop.runtime.clearData.apply({ previewId: currentPreviewId, confirmationText: clearConfirmation.value })
  status.textContent = `${result.state} · ${result.code}`
  clearExplanation.textContent = result.message
  if (result.state !== "PREPARED") clearApply.disabled = false
})

void refresh()

