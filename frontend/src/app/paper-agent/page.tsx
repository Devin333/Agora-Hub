import type { Metadata } from "next"
import { Suspense } from "react"
import { PaperAgentApp } from "@/features/paper-agent-app/paper-agent-app"

export const metadata: Metadata = {
  title: "Agora · 论文工作台",
  description: "使用真实论文库、原文、笔记、研究运行与个人记忆的论文桌面工作台。",
}

export const dynamic = "force-dynamic"

export default function PaperAgentPage() {
  return <Suspense fallback={<main aria-busy="true">正在打开 Agora 论文工作台…</main>}><PaperAgentApp /></Suspense>
}
