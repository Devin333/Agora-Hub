"use client"

import Link from "next/link"
import { BookOpen } from "lucide-react"
import styles from "@/features/portal/reader/reader-workspace.module.css"

export default function ReaderError({ reset }: { reset: () => void }) {
  return <div className={styles.loading} role="alert"><BookOpen size={26} /><h1>暂时无法打开论文</h1><p>请稍后重试，已保存的本地笔记仍会保留。</p><div className={styles.statusActions}><button type="button" onClick={reset}>重新加载</button><Link href="/design-demo/papers">返回论文列表</Link></div></div>
}
