import type { ReactNode } from "react"
import localFont from "next/font/local"
import styles from "./layout.module.css"

// Geist variable font (SIL OFL 1.1), vendored next to the workspace so builds never resolve fonts through node_modules.
const geistSans = localFont({
  src: "../../features/paper-agent-app/fonts/Geist-Variable.woff2",
  variable: "--font-geist-sans",
  weight: "100 900",
  display: "swap",
})

/** Scopes the workspace UI typeface to the paper-agent route without touching the portal. */
export default function PaperAgentLayout({ children }: { children: ReactNode }) {
  return <div className={`${geistSans.variable} ${styles.fontScope}`}>{children}</div>
}
