"use strict"

// Packaged connection page. It shows whenever the workspace does not: before a
// connection is chosen, while connecting (or starting the local stack), after a
// failure, while the local stack waits for its first administrator and while
// the user switches connection. Every decision is the host's; this page only
// renders the status it receives and forwards the user's choices.
const bridge = window.agoraConnection
const byId = id => document.getElementById(id)
const elements = {
  title: byId("title"),
  message: byId("message"),
  status: byId("status"),
  retry: byId("retry"),
  change: byId("change"),
  dockerDownload: byId("docker-download"),
  localCancel: byId("local-cancel"),
  localProgress: byId("local-progress"),
  localSteps: byId("local-steps"),
  localBar: byId("local-bar"),
  localBarLabel: byId("local-bar-label"),
  localDetails: byId("local-details"),
  localOutput: byId("local-output"),
  choiceForm: byId("choice-form"),
  choiceOfficial: byId("choice-official"),
  serverFields: byId("server-fields"),
  localFields: byId("local-fields"),
  address: byId("address"),
  registry: byId("registry"),
  choiceError: byId("choice-error"),
  choose: byId("choose"),
  cancel: byId("cancel"),
  adminForm: byId("admin-form"),
  adminUsername: byId("admin-username"),
  adminPassword: byId("admin-password"),
  adminConfirmation: byId("admin-confirmation"),
  adminError: byId("admin-error"),
  adminSubmit: byId("admin-submit"),
  adminChange: byId("admin-change"),
  origin: byId("origin")
}

const LOCAL_STEPS = [
  { id: "checking", label: "检查 Docker" },
  { id: "preparing", label: "准备配置" },
  { id: "pulling", label: "下载服务镜像" },
  { id: "starting", label: "启动服务" },
  { id: "verifying", label: "确认服务就绪" }
]
const LOCAL_WORKING = new Set(["checking", "preparing", "pulling", "starting", "verifying"])
const DOCKER_FAILURES = new Set(["docker_missing"])

let current = null
let pending = false
let countdown = null
// The user left the administrator form to pick another connection; it stays
// hidden until they choose local mode again.
let adminDismissed = false

function hostOf(origin) {
  try {
    return new URL(origin).host
  } catch {
    return origin
  }
}

function choosing(status) {
  return status.phase === "unconfigured" || status.phase === "editing"
}

function localWorking(status) {
  return LOCAL_WORKING.has(status.local.phase)
}

/** The local stack's progress is shown while it starts for a choice or for the saved local mode. */
function showsLocalProgress(status) {
  if (!localWorking(status)) return false
  return choosing(status) ? selectedMode() === "local" : status.mode === "local"
}

function needsAdmin(status) {
  if (status.phase === "local-setup") return true
  return choosing(status) && !adminDismissed && (status.local.phase === "needs-admin" || status.local.phase === "creating-admin")
}

function selectedMode() {
  const checked = elements.choiceForm.querySelector("input[name=mode]:checked")
  return checked ? checked.value : null
}

function render() {
  const status = current
  if (!status) return
  const admin = needsAdmin(status)
  const progress = showsLocalProgress(status)
  const form = choosing(status) && !admin && !progress

  elements.choiceForm.hidden = !form
  elements.adminForm.hidden = !admin
  elements.localProgress.hidden = !progress
  elements.localCancel.hidden = !progress
  elements.choiceOfficial.hidden = !status.officialOrigin
  elements.cancel.hidden = status.phase !== "editing"
  elements.retry.hidden = form || admin || progress || status.phase !== "failed"
  elements.change.hidden = form || admin || progress || (status.phase !== "failed" && status.phase !== "connecting")
  const dockerMissing = status.local.failure && DOCKER_FAILURES.has(status.local.failure.code)
  elements.dockerDownload.hidden = !dockerMissing || admin
  elements.origin.hidden = form || admin || !status.origin
  elements.origin.textContent = status.origin ? (status.mode === "local" ? `本机服务：${status.origin}` : `服务器：${status.origin}`) : ""
  renderChoiceFields()
  renderLocalDetails(status)
  setDisabled(pending)

  if (admin) {
    elements.title.textContent = "设置本机管理员"
    elements.message.textContent = "本机服务已经启动。请设置第一个管理员账号，之后用它登录工作台。"
    elements.status.hidden = true
    elements.adminSubmit.textContent = pending || status.local.phase === "creating-admin" ? "正在创建…" : "创建并打开工作台"
    stopCountdown()
    return
  }
  if (progress) {
    renderLocalProgress(status)
    return
  }
  if (form) {
    elements.title.textContent = status.phase === "unconfigured" ? "选择连接方式" : "切换连接方式"
    elements.message.textContent = status.phase === "unconfigured"
      ? "论文工作台可以连接服务器使用，也可以在这台电脑上运行。"
      : "确认新的连接方式可用后才会切换，原来的设置在那之前保持不变。"
    elements.status.hidden = true
    elements.choose.textContent = pending ? (selectedMode() === "local" ? "正在启动本机服务…" : "正在验证服务器…") : "连接"
    stopCountdown()
    return
  }

  elements.status.hidden = false
  if (status.phase === "failed" && status.failure) {
    elements.title.textContent = status.mode === "local" ? "本机服务不可用" : "无法连接服务器"
    elements.message.textContent = status.local.phase === "stopping" ? "正在停止本机服务…" : status.failure.message
    startCountdown(status.nextRetryAt)
  } else if (status.phase === "connected") {
    elements.title.textContent = "已连接"
    elements.message.textContent = "正在打开论文工作台。"
    elements.status.textContent = status.mode === "local" ? "本机服务" : hostOf(status.origin)
    stopCountdown()
  } else {
    elements.title.textContent = status.mode === "local" ? "正在打开本机服务" : "正在连接服务器"
    elements.message.textContent = "正在打开论文工作台。"
    elements.status.textContent = status.origin && status.mode !== "local" ? `正在连接 ${hostOf(status.origin)}…` : "连接中"
    stopCountdown()
  }
}

function renderChoiceFields() {
  const mode = selectedMode()
  elements.serverFields.hidden = mode !== "server"
  elements.localFields.hidden = mode !== "local"
}

function renderLocalProgress(status) {
  const local = status.local
  elements.title.textContent = "正在启动本机服务"
  elements.message.textContent = local.phase === "pulling"
    ? "第一次运行或升级后需要下载服务镜像，可能要几分钟。可以关闭窗口，完成后从托盘打开。"
    : "正在这台电脑上启动论文工作台的服务。"
  elements.status.hidden = true
  const activeIndex = LOCAL_STEPS.findIndex(step => step.id === local.phase)
  elements.localSteps.replaceChildren(...LOCAL_STEPS.map((step, index) => {
    const item = document.createElement("li")
    item.textContent = step.label
    item.className = index < activeIndex ? "done" : index === activeIndex ? "active" : ""
    if (index === activeIndex) item.setAttribute("aria-current", "step")
    return item
  }))
  const pulling = local.phase === "pulling"
  elements.localBar.hidden = !pulling
  elements.localBarLabel.hidden = !pulling
  if (pulling) {
    if (typeof local.progress === "number") {
      elements.localBar.value = local.progress
      elements.localBarLabel.textContent = `已下载 ${Math.round(local.progress * 100)}%`
    } else {
      elements.localBar.removeAttribute("value")
      elements.localBarLabel.textContent = "正在准备下载…"
    }
  }
  stopCountdown()
}

function renderLocalDetails(status) {
  const relevant = status.mode === "local" || (choosing(status) && selectedMode() === "local") || status.phase === "local-setup"
  const lines = status.local.output
  elements.localDetails.hidden = !relevant || lines.length === 0
  const atBottom = elements.localOutput.scrollTop + elements.localOutput.clientHeight >= elements.localOutput.scrollHeight - 4
  elements.localOutput.textContent = lines.join("\n")
  if (atBottom) elements.localOutput.scrollTop = elements.localOutput.scrollHeight
}

function renderRegistries(status) {
  const options = status.local.registries
  if (elements.registry.options.length !== options.length) {
    elements.registry.replaceChildren(...options.map(registry => {
      const option = document.createElement("option")
      option.value = registry.id
      option.textContent = registry.label
      return option
    }))
  }
  elements.registry.value = status.local.registry
}

function setDisabled(disabled) {
  for (const element of [elements.choose, elements.address, elements.registry, elements.cancel, elements.adminSubmit, elements.adminUsername, elements.adminPassword, elements.adminConfirmation, elements.adminChange]) {
    element.disabled = disabled
  }
  for (const input of elements.choiceForm.querySelectorAll("input[name=mode]")) input.disabled = disabled
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
  const previous = current
  current = status
  renderRegistries(status)
  if (choosing(status) && previous?.phase !== status.phase) prefillChoice(status)
  if (needsAdmin(status) && !(previous && needsAdmin(previous))) prefillAdmin()
  render()
}

function prefillChoice(status) {
  const mode = status.mode ?? (status.officialOrigin ? "official" : "server")
  const input = elements.choiceForm.querySelector(`input[name=mode][value=${mode}]`)
  if (input) input.checked = true
  elements.address.value = status.serverOrigin ?? ""
  setError(elements.choiceError, "")
  queueMicrotask(() => (mode === "server" ? elements.address : input)?.focus())
}

function prefillAdmin() {
  elements.adminPassword.value = ""
  elements.adminConfirmation.value = ""
  setError(elements.adminError, "")
  queueMicrotask(() => elements.adminUsername.focus())
}

function setError(element, message) {
  element.textContent = message
}

function currentChoice() {
  const mode = selectedMode()
  if (mode === "server") return { mode, address: elements.address.value }
  if (mode === "local") return { mode, registry: elements.registry.value }
  return { mode }
}

async function run(action, errorElement, focusOnError) {
  if (pending) return
  pending = true
  setError(errorElement, "")
  render()
  try {
    const result = await action()
    // The admin form replaces the choices on its own; its prompt needs no error line.
    if (result && result.ok === false && result.code !== "local_admin_required") {
      setError(errorElement, result.message)
      focusOnError?.focus()
    }
  } catch {
    setError(errorElement, "操作没有完成，请重试。")
  } finally {
    pending = false
    render()
  }
}

elements.choiceForm.addEventListener("change", event => {
  if (event.target.name === "mode") {
    setError(elements.choiceError, "")
    render()
    if (event.target.value === "server") elements.address.focus()
  }
})
elements.choiceForm.addEventListener("submit", event => {
  event.preventDefault()
  const choice = currentChoice()
  if (!choice.mode) {
    setError(elements.choiceError, "请选择一种连接方式。")
    return
  }
  if (choice.mode === "local") adminDismissed = false
  void run(() => bridge.choose(choice), elements.choiceError, choice.mode === "server" ? elements.address : null)
})
elements.adminForm.addEventListener("submit", event => {
  event.preventDefault()
  const request = { username: elements.adminUsername.value, password: elements.adminPassword.value, confirmation: elements.adminConfirmation.value }
  void run(() => bridge.local.createAdmin(request), elements.adminError, elements.adminUsername)
})
elements.cancel.addEventListener("click", () => void bridge.cancelEdit().then(apply))
elements.localCancel.addEventListener("click", () => void bridge.local.cancel().then(apply))
// Editing is a host state, so no automatic retry replaces the form while the user chooses.
elements.change.addEventListener("click", () => void bridge.edit().then(apply))
elements.adminChange.addEventListener("click", () => {
  adminDismissed = true
  void bridge.edit().then(apply)
})
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
