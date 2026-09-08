import { BookOpen } from "lucide-react"
import styles from "@/features/portal/reader/reader-workspace.module.css"

export default function ReaderLoading() {
  return <div className={styles.loading} role="status"><BookOpen size={26} /><h1>正在打开论文</h1><p>加载论文信息与阅读内容…</p></div>
}
