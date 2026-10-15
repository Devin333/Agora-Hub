import path from "node:path"

const SENSITIVE_KEY = /(api[-_]?key|token|secret|password|authorization|cookie|session|credential)/i
const SENSITIVE_VALUE = /(?:bearer\s+[a-z0-9._~+\/-]+=*|sk-[a-z0-9_-]{12,}|(?:api[-_]?key|token|secret|password)\s*[=:]\s*[^\s,;]+)/gi
const WINDOWS_USER_PATH = /[a-z]:\\users\\[^\\\s]+/gi

export function redactText(value: unknown, maxLength = 1000): string {
  const text = String(value ?? "")
    .replace(SENSITIVE_VALUE, "[REDACTED]")
    .replace(WINDOWS_USER_PATH, "%USERPROFILE%")
  return text.length <= maxLength ? text : `${text.slice(0, maxLength)}…`
}

export function redactRecord(input: Record<string, unknown>, depth = 0): Record<string, unknown> {
  if (depth > 4) return { truncated: true }
  const output: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(input)) {
    if (SENSITIVE_KEY.test(key)) {
      output[key] = "[REDACTED]"
    } else if (Array.isArray(value)) {
      output[key] = value.slice(0, 50).map(item => {
        if (item && typeof item === "object") return redactRecord(item as Record<string, unknown>, depth + 1)
        return typeof item === "string" ? redactText(item) : item
      })
    } else if (value && typeof value === "object") {
      output[key] = redactRecord(value as Record<string, unknown>, depth + 1)
    } else {
      output[key] = typeof value === "string" ? redactText(value) : value
    }
  }
  return output
}

export function safeFileLabel(filePath: string): string {
  return redactText(path.basename(filePath), 160)
}
