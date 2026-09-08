"use client"

import Link from "next/link"
import { WandSparkles } from "lucide-react"
import { PortalAccountControl } from "@/components/auth/portal-account-control"
import type { Locale } from "@/lib/papers/types"
import styles from "./papers-design-demo.module.css"

export function PaperPortalHeader({ locale }: { locale: Locale }) {
  const zh = locale === "zh"
  return <header className={styles.header}><nav className={styles.headerInner} aria-label={zh ? "主导航" : "Main navigation"}>
    <Link href="/design-demo" className={styles.brand} aria-label="Agora AI"><span className={styles.brandIcon}><WandSparkles size={19} /></span><span>Agora<span className={styles.accent}>AI</span></span></Link>
    <div className={styles.navigation}><Link href="/design-demo">{zh ? "首页" : "Home"}</Link><Link href="/design-demo/papers" aria-current="true">{zh ? "论文研究" : "Papers"}</Link><Link href="/projects">{zh ? "项目雷达" : "Projects"}</Link><Link href="/community">{zh ? "社区信号" : "Community"}</Link></div>
    <div className={styles.headerActions}><PortalAccountControl /><Link href="/design-demo#workspace" className={styles.newResearch}><WandSparkles size={16} />{zh ? "开始研究" : "New research"}</Link></div>
  </nav></header>
}
