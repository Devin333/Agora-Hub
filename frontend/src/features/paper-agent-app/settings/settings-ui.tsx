import type { ReactNode } from "react"
import { AlertTriangle, LoaderCircle } from "lucide-react"
import styles from "../paper-agent-settings-panel.module.css"

// Presentational pieces shared by every settings section.

export type Tone = "success" | "warning" | "danger" | "neutral"

const badgeTone: Record<Tone, string> = {
  success: styles.badgeSuccess,
  warning: styles.badgeWarning,
  danger: styles.badgeDanger,
  neutral: styles.badgeNeutral,
}

export const cx = (...values: Array<string | false | null | undefined>) => values.filter(Boolean).join(" ")

export function SectionHeading({ icon, title, id }: { icon: ReactNode; title: string; id: string }) {
  return <div className={styles.sectionHeading}>{icon}<h3 id={id}>{title}</h3></div>
}

export function StatusBadge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={cx(styles.badge, badgeTone[tone])}>{children}</span>
}

export function InlineError({ children }: { children: ReactNode }) {
  return <p className={styles.inlineError} role="alert"><AlertTriangle size={16} />{children}</p>
}

export function LoadingLine({ label = "正在读取…" }: { label?: string }) {
  return <p className={styles.loadingLine}><LoaderCircle className={styles.spinner} size={16} />{label}</p>
}

export function Notice({ icon, title, children, warning = false }: { icon: ReactNode; title: string; children: ReactNode; warning?: boolean }) {
  return <div className={warning ? styles.gapNotice : styles.notice}>{icon}<div><strong>{title}</strong><p>{children}</p></div></div>
}

export function SummaryCard({ icon, label, value, tone }: { icon: ReactNode; label: string; value: string; tone: Tone }) {
  return <div className={cx(styles.summaryCard, styles[tone])}>{icon}<div><span>{label}</span><strong>{value}</strong></div></div>
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "—"
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return "时间未知"
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(parsed)
}
