"use client"

import { useEffect, useState } from "react"
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
      <main className="grid min-h-screen place-items-center bg-[#f7f4fb]" role="status">
        正在核对账号与研究工作区…
      </main>
    )
  }
  if (!workspace.owner) {
    if (fallback) {
      return <PaperReaderWorkspace payload={fallback} backHref={backHref} />
    }
    return (
      <main className="grid min-h-screen place-items-center bg-[#f7f4fb] px-6">
        <section className="max-w-lg rounded-xl border bg-white p-6 text-center">
          <h1 className="text-xl font-semibold">需要登录</h1>
          <p className="mt-3 text-sm text-muted-foreground">
            这篇论文没有可用的公开正文。登录后可读取账号内的私有导入。
          </p>
          <a
            className="mt-5 inline-block text-sm text-primary"
            href={`/login?next=${encodeURIComponent(`/papers/${paperId}/read`)}`}
          >
            前往登录
          </a>
        </section>
      </main>
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
      <main className="grid min-h-screen place-items-center bg-[#f7f4fb]" role="status">
        正在读取论文与来源修订…
      </main>
    )
  }
  if (!payload) {
    return (
      <main className="grid min-h-screen place-items-center bg-[#f7f4fb] px-6">
        <section className="max-w-lg rounded-xl border bg-white p-6 text-center">
          <h1 className="text-xl font-semibold">论文暂时不可阅读</h1>
          <p role="alert" className="mt-3 text-sm text-muted-foreground">
            {error || "尚未取得通过完整性校验的正文或可用原 PDF。"}
          </p>
          <div className="mt-5 flex justify-center gap-3">
            <button
              type="button"
              className="text-sm text-primary underline"
              onClick={() => setAttempt(value => value + 1)}
            >
              重新获取
            </button>
            <a className="text-sm text-primary" href={backHref}>返回论文列表</a>
          </div>
        </section>
      </main>
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
        <div
          role="status"
          className="fixed bottom-4 left-1/2 z-50 -translate-x-1/2 rounded-lg border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900"
        >
          当前显示已通过公开读取路径取得的正文；私有阅读投影没有载入。{error}
          <button
            type="button"
            className="ml-2 underline"
            onClick={() => setAttempt(value => value + 1)}
          >
            重试
          </button>
        </div>
      )}
      {error && payloadSource === "private" && (
        <div
          role="status"
          className="fixed bottom-4 left-1/2 z-50 -translate-x-1/2 rounded-lg border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900"
        >
          刷新失败，当前仍显示本账号本次页面已取得的阅读投影。{error}
          <button
            type="button"
            className="ml-2 underline"
            onClick={() => setAttempt(value => value + 1)}
          >
            重试
          </button>
        </div>
      )}
    </>
  )
}
