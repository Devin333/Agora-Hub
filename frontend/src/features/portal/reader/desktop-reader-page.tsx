"use client"

import { useEffect, useState, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import {
  DesktopResearchError,
  fetchDesktopReader,
} from "@/lib/research/desktop/client"
import type { ReaderWorkspacePayload } from "@/features/portal/reader/reader-contract"
import { PaperReaderWorkspace } from "@/features/portal/reader/paper-reader-workspace"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"

export function DesktopReaderPage({
  paperId,
  fallback,
  backHref,
}: {
  paperId: string
  fallback: ReaderWorkspacePayload | null
  backHref: string
}) {
  const workspace = useOwnedResearchWorkspace()
  if (!workspace.ready) {
    return (
      <ReaderStatusScreen>正在核对账号与研究工作区…</ReaderStatusScreen>
    )
  }
  if (!workspace.owner) {
    if (fallback) {
      return <PaperReaderWorkspace payload={fallback} backHref={backHref} />
    }
    return (
      <ReaderMessageScreen
        title="需要登录"
        description={<p className="mt-2 text-body2 text-muted-foreground">这篇论文没有可用的公开正文。登录后可读取账号内的私有导入。</p>}
      >
        <Button asChild>
          <a href={`/login?next=${encodeURIComponent(`/papers/${paperId}/read`)}`}>前往登录</a>
        </Button>
      </ReaderMessageScreen>
    )
  }
  const stablePaperId = fallback?.paper.id || paperId
  return (
    <OwnedDesktopReader
      key={`${workspace.owner}:${stablePaperId}`}
      owner={workspace.owner}
      paperId={stablePaperId}
      fallback={fallback}
      backHref={backHref}
    />
  )
}

function OwnedDesktopReader({
  owner,
  paperId,
  fallback,
  backHref,
}: {
  owner: string
  paperId: string
  fallback: ReaderWorkspacePayload | null
  backHref: string
}) {
  const workspace = useOwnedResearchWorkspace()
  const [payload, setPayload] = useState<ReaderWorkspacePayload | null>(fallback)
  const [payloadSource, setPayloadSource] = useState<"public" | "private" | "none">(
    fallback ? "public" : "none",
  )
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    setPayload(fallback)
    setPayloadSource(fallback ? "public" : "none")
    setLoading(true)
    setError("")
    const controller = new AbortController()
    fetchDesktopReader(paperId, controller.signal)
      .then(value => {
        if (workspace.owner !== owner) return
        setPayload({ ...value, aiReady: value.capabilities?.ask === true })
        setPayloadSource("private")
        setError("")
      })
      .catch(cause => {
        if (controller.signal.aborted || workspace.owner !== owner) return
        if (cause instanceof DesktopResearchError && [401, 403].includes(cause.status ?? 0)) {
          setPayload(fallback)
          setPayloadSource(fallback ? "public" : "none")
        }
        setError(cause instanceof Error ? cause.message : "暂时无法读取论文。")
      })
      .finally(() => {
        if (!controller.signal.aborted && workspace.owner === owner) setLoading(false)
      })
    return () => controller.abort()
  }, [attempt, fallback, owner, paperId, workspace.owner])

  if (!payload && loading) {
    return (
      <ReaderStatusScreen>正在读取论文与来源修订…</ReaderStatusScreen>
    )
  }
  if (!payload) {
    return (
      <ReaderMessageScreen
        title="论文暂时不可阅读"
        description={(
          <p role="alert" className="mt-2 text-body2 text-muted-foreground">
            {error || "尚未取得通过完整性校验的正文或可用原 PDF。"}
          </p>
        )}
      >
        <Button type="button" onClick={() => setAttempt(value => value + 1)}>重新获取</Button>
        <Button asChild variant="outline">
          <a href={backHref}>返回论文列表</a>
        </Button>
      </ReaderMessageScreen>
    )
  }

  return (
    <>
      <PaperReaderWorkspace
        key={`${payload.paper.id}:${payload.sourceRevision ?? "public"}`}
        payload={payload}
        backHref={backHref}
      />
      {error && payloadSource === "public" && (
        <ReaderNotice onRetry={() => setAttempt(value => value + 1)}>
          当前显示已通过公开读取路径取得的正文；私有阅读投影没有载入。{error}
        </ReaderNotice>
      )}
      {error && payloadSource === "private" && (
        <ReaderNotice onRetry={() => setAttempt(value => value + 1)}>
          刷新失败，当前仍显示本账号本次页面已取得的阅读投影。{error}
        </ReaderNotice>
      )}
    </>
  )
}

function ReaderStatusScreen({ children }: { children: ReactNode }) {
  return (
    <main className="grid min-h-screen place-items-center bg-background px-6 text-body2 text-muted-foreground" role="status">
      <span className="text-shimmer">{children}</span>
    </main>
  )
}

function ReaderMessageScreen({
  title,
  description,
  children,
}: {
  title: string
  description: ReactNode
  children: ReactNode
}) {
  return (
    <main className="grid min-h-screen place-items-center bg-background px-6">
      <section className="w-full max-w-lg rounded-xl bg-card p-6 text-center text-card-foreground ring-1 ring-foreground/10">
        <h1 className="text-heading3 font-semibold">{title}</h1>
        {description}
        <div className="mt-5 flex flex-wrap justify-center gap-2">{children}</div>
      </section>
    </main>
  )
}

function ReaderNotice({ children, onRetry }: { children: ReactNode; onRetry: () => void }) {
  return (
    <div
      role="status"
      className="fixed bottom-4 left-1/2 z-50 flex max-w-[min(42rem,calc(100vw-2rem))] -translate-x-1/2 items-center gap-3 rounded-xl border border-warning/30 bg-popover px-4 py-2.5 text-body2 text-popover-foreground shadow-md"
    >
      <span className="size-1.5 shrink-0 rounded-full bg-warning" aria-hidden="true" />
      <span className="min-w-0">{children}</span>
      <Button type="button" variant="outline" size="sm" className="shrink-0" onClick={onRetry}>重试</Button>
    </div>
  )
}
