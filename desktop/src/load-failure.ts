/**
 * Why the workspace on the server could not be shown, in words a user can act on.
 *
 * Chromium reports a failed load as a `net::ERR_*` name (did-fail-load's
 * description, or the message of a rejected `net.fetch`); the server itself can
 * answer with an HTTP status or with something that is not this service; in
 * local mode Docker can be missing, stopped or unable to run the stack. Each
 * maps to one fixed category; raw upstream text is never shown in the message
 * (the local stack's own output is shown separately, on request).
 */
export type ConnectionFailureCode =
  | "offline"
  | "dns"
  | "refused"
  | "timeout"
  | "reset"
  | "certificate"
  | "tls"
  | "proxy"
  | "server_unavailable"
  | "not_agora"
  | "redirected"
  | "page_crashed"
  | "unexpected"
  | "docker_missing"
  | "docker_not_running"
  | "docker_permission"
  | "local_port_in_use"
  | "image_unavailable"
  | "image_pull_failed"
  | "disk_full"
  | "local_stack_failed"
  | "local_stack_timeout"
  | "local_stack_stopped"

export interface ConnectionFailure {
  code: ConnectionFailureCode
  message: string
  /** Transient failures are retried on their own; the others wait for the user. */
  retryable: boolean
}

const FAILURES: Readonly<Record<ConnectionFailureCode, Omit<ConnectionFailure, "code">>> = Object.freeze({
  offline: { message: "这台电脑当前没有网络连接。网络恢复后会自动重新连接。", retryable: true },
  dns: { message: "找不到这个服务器地址。请检查地址是否拼写正确，或稍后重试。", retryable: true },
  refused: { message: "服务器拒绝了连接，服务可能尚未启动。请稍后重试或联系管理员。", retryable: true },
  timeout: { message: "连接服务器超时。请检查网络，或稍后重试。", retryable: true },
  reset: { message: "与服务器的连接被中断。请稍后重试。", retryable: true },
  certificate: { message: "服务器的安全证书无效或已过期，为保护你的账号已停止连接。请联系管理员更新证书。", retryable: false },
  tls: { message: "无法与服务器建立安全连接。请确认地址使用 HTTPS，或联系管理员。", retryable: false },
  proxy: { message: "无法通过系统代理连接服务器。请检查代理设置。", retryable: true },
  server_unavailable: { message: "服务器暂时不可用，可能正在升级或维护。稍后会自动重新连接。", retryable: true },
  not_agora: { message: "这个地址上没有找到 Agora 服务。请确认填写的是管理员提供的服务器地址。", retryable: false },
  redirected: { message: "服务器把连接转到了另一个地址，为安全起见已停止。请确认填写的服务器地址正确。", retryable: false },
  page_crashed: { message: "工作台页面意外停止。正在重新连接。", retryable: true },
  unexpected: { message: "连接服务器时出现问题。请稍后重试。", retryable: true },
  docker_missing: { message: "这台电脑上没有可用的 Docker。请先安装 Docker Desktop（Linux 上是 Docker Engine 和 Compose 插件），启动后点“立即重试”。", retryable: false },
  docker_not_running: { message: "Docker 没有运行。请打开 Docker Desktop，等它启动完成后会自动继续。", retryable: true },
  docker_permission: { message: "当前用户没有权限使用 Docker。请把当前用户加入 docker-users（Windows）或 docker 组（macOS、Linux），重新登录系统后重试。", retryable: false },
  local_port_in_use: { message: "本机端口被其他程序占用，换了端口仍然无法启动。请关闭占用端口的程序后重试。", retryable: false },
  image_unavailable: { message: "镜像仓库里没有这个版本的服务镜像，或拒绝了访问。请换一个镜像下载源后重试。", retryable: false },
  image_pull_failed: { message: "下载服务镜像失败。请检查网络，或换一个镜像下载源。稍后会自动重试。", retryable: true },
  disk_full: { message: "磁盘空间不足，无法下载或启动本机服务。请清理磁盘后重试。", retryable: false },
  local_stack_failed: { message: "本机服务没有正常启动。可以展开详细输出查看原因，或在托盘菜单里查看本机服务日志。", retryable: false },
  local_stack_timeout: { message: "本机服务启动超时。可以展开详细输出查看原因后重试。", retryable: false },
  local_stack_stopped: { message: "本机服务已停止。点击“立即重试”重新启动。", retryable: false }
})

const NETWORK_ERRORS: ReadonlyArray<readonly [ConnectionFailureCode, readonly string[]]> = [
  ["offline", ["ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED", "ERR_NETWORK_IO_SUSPENDED"]],
  ["dns", ["ERR_NAME_NOT_RESOLVED", "ERR_NAME_RESOLUTION_FAILED", "ERR_ADDRESS_INVALID"]],
  ["refused", ["ERR_CONNECTION_REFUSED", "ERR_ADDRESS_UNREACHABLE", "ERR_CONNECTION_FAILED"]],
  ["timeout", ["ERR_TIMED_OUT", "ERR_CONNECTION_TIMED_OUT"]],
  ["reset", ["ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_ABORTED", "ERR_EMPTY_RESPONSE", "ERR_HTTP2_PROTOCOL_ERROR"]],
  ["tls", ["ERR_SSL_PROTOCOL_ERROR", "ERR_SSL_VERSION_OR_CIPHER_MISMATCH", "ERR_BAD_SSL_CLIENT_AUTH_CERT"]],
  ["proxy", ["ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED", "ERR_PROXY_CERTIFICATE_INVALID", "ERR_PROXY_AUTH_UNSUPPORTED"]]
]

export function connectionFailure(code: ConnectionFailureCode): ConnectionFailure {
  return { code, ...FAILURES[code] }
}

/** The `ERR_*` name inside a Chromium error description or message, if any. */
export function chromiumErrorName(text: string): string | null {
  const match = /\b(ERR_[A-Z0-9_]+)\b/.exec(text)
  return match ? match[1] : null
}

export function describeNetworkError(text: string): ConnectionFailure {
  const name = chromiumErrorName(text)
  if (!name) return connectionFailure("unexpected")
  if (name.startsWith("ERR_CERT_") || name === "ERR_SSL_PINNED_KEY_NOT_IN_CERT_CHAIN") return connectionFailure("certificate")
  for (const [code, names] of NETWORK_ERRORS) {
    if (names.includes(name)) return connectionFailure(code)
  }
  return connectionFailure("unexpected")
}

/** A main-frame answer that cannot be the workspace; null means the status is fine to show. */
export function describeHttpStatus(status: number): ConnectionFailure | null {
  if (status >= 500) return connectionFailure("server_unavailable")
  if (status === 404) return connectionFailure("not_agora")
  return null
}

/** did-fail-load also fires when a newer navigation replaced the pending one; that is not a failure. */
export function isSupersededLoad(description: string): boolean {
  return chromiumErrorName(description) === "ERR_ABORTED"
}
