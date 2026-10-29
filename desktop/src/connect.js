"use strict"

// Packaged connection page. It shows whenever the workspace on the server does
// not: before an address exists, while connecting, after a failure and while
// the user changes the address. Every decision is the host's; this page only
// renders the status it receives and forwards the user's choices.
const bridge = window.agoraConnection
const elements = {
  title: document.getElementById("title"),
  message: document.getElementById("message"),
  status: document.getElementById("status"),
  retry: document.getElementById("retry"),
  change: document.getElementById("change"),
  form: document.getElementById("address-form"),
  address: document.getElementById("address"),
  error: document.getElementById("address-error"),
  save: document.getElementById("save"),
  cancel: document.getElementById("cancel"),
  reset: document.getElementById("reset"),
  origin: document.getElementById("origin")
}

let current = null
let pending = false
let countdown = null

function hostOf(origin) {
  try {
    return new URL(origin).host
  } catch {
    return origin
  }
}

function showForm(status) {
  return status.phase === "unconfigured" || status.phase === "editing"
}

function render() {
  const status = current
  if (!status) return
  const formVisible = showForm(status)
  elements.form.hidden = !formVisible
  elements.retry.hidden = formVisible || status.phase !== "failed"
  elements.change.hidden = formVisible || (status.phase !== "failed" && status.phase !== "connecting")
  elements.cancel.hidden = status.phase !== "editing"
  elements.reset.hidden = !(status.source === "user" && status.bundledOrigin)
  elements.origin.hidden = formVisible || !status.origin
  elements.origin.textContent = status.origin ? `服务器：${status.origin}` : ""
  elements.save.disabled = pending
  elements.save.textContent = pending ? "正在验证服务器…" : "连接"
  elements.address.disabled = pending
  elements.reset.disabled = pending
  elements.cancel.disabled = pending

  if (formVisible) {
    elements.title.textContent = status.phase === "unconfigured" ? "连接到 Agora 服务器" : "更换服务器地址"
    elements.message.textContent = status.phase === "unconfigured"
      ? "论文工作台运行在服务器上。请输入管理员提供的服务器地址，然后用你的账号登录。"
      : "保存后会验证新地址，并在新的服务器上重新登录。"
    elements.status.hidden = true
    stopCountdown()
    return
  }

  elements.status.hidden = false
  if (status.phase === "failed" && status.failure) {
    elements.title.textContent = "无法连接服务器"
    elements.message.textContent = status.failure.message
    startCountdown(status.nextRetryAt)
  } else if (status.phase === "connected") {
    elements.title.textContent = "已连接"
    elements.message.textContent = "正在打开论文工作台。"
    elements.status.textContent = hostOf(status.origin)
    stopCountdown()
  } else {
    elements.title.textContent = "正在连接服务器"
    elements.message.textContent = "正在打开论文工作台。"
    elements.status.textContent = status.origin ? `正在连接 ${hostOf(status.origin)}…` : "连接中"
    stopCountdown()
  }
}

function startCountdown(nextRetryAt) {
  stopCountdown()
  const tick = () => {
    if (!nextRetryAt) {
      elements.status.textContent = "需要你处理后再重试"
      return
    }
    const seconds = Math.max(0, Math.ceil((nextRetryAt - Date.now()) / 1000))
    elements.status.textContent = seconds > 0 ? `${seconds} 秒后自动重试` : "正在重试…"
  }
  tick()
  if (nextRetryAt) countdown = setInterval(tick, 1000)
}

function stopCountdown() {
  if (countdown !== null) clearInterval(countdown)
  countdown = null
}

function apply(status) {
  const previousPhase = current?.phase
  current = status
  if (showForm(status) && previousPhase !== status.phase) prefill(status)
  render()
}

function prefill(status) {
  elements.address.value = status.origin ?? status.bundledOrigin ?? ""
  setError("")
  queueMicrotask(() => elements.address.focus())
}

function setError(message) {
  elements.error.textContent = message
  elements.address.setAttribute("aria-invalid", message ? "true" : "false")
}

async function run(action) {
  if (pending) return
  pending = true
  setError("")
  render()
  try {
    const result = await action()
    if (result && result.ok === false) {
      setError(result.message)
      elements.address.focus()
    }
  } catch {
    setError("操作没有完成，请重试。")
  } finally {
    pending = false
    render()
  }
}

elements.form.addEventListener("submit", event => {
  event.preventDefault()
  void run(() => bridge.save(elements.address.value))
})
elements.reset.addEventListener("click", () => void run(() => bridge.reset()))
elements.cancel.addEventListener("click", () => void bridge.cancelEdit().then(apply))
// Editing is a host state, so no automatic retry replaces the form while the user types.
elements.change.addEventListener("click", () => void bridge.edit().then(apply))
elements.retry.addEventListener("click", () => void bridge.retry().then(apply))
// A failure caused by a dropped network clears up on its own once the network is back.
window.addEventListener("online", () => {
  if (current?.phase === "failed") void bridge.retry().then(apply)
})

if (bridge) {
  bridge.onChange(apply)
  void bridge.status().then(apply)
} else {
  elements.title.textContent = "无法打开连接页"
  elements.message.textContent = "应用组件不完整，请重新安装 Agora Paper Agent。"
  elements.status.hidden = true
}
