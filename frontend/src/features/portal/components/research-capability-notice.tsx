"use client"

import { useQuery } from "@tanstack/react-query"
import {
  AlertTriangle,
  CheckCircle2,
  CircleHelp,
  RefreshCw,
  WifiOff,
} from "lucide-react"
import { isLocalDesktopProfile } from "@/lib/deployment-profile"
import { fetchDesktopCapabilities } from "@/lib/research/desktop/client"
import type { DesktopCapabilities } from "@/lib/research/desktop/contracts"
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history"

export function ResearchCapabilityNotice({ compact = false }: { compact?: boolean }) {
  if (!isLocalDesktopProfile()) return null
  return <DesktopResearchCapabilityNotice compact={compact} />
}

function DesktopResearchCapabilityNotice({ compact }: { compact: boolean }) {
  const workspace = useOwnedResearchWorkspace()
  const query = useQuery({
    queryKey: ["research", "desktop", "capabilities", workspace.owner ?? "guest"],
    queryFn: ({ signal }) => fetchDesktopCapabilities(signal),
    retry: false,
    enabled: workspace.ready && Boolean(workspace.owner),
    staleTime: 30_000,
  })

  if (!workspace.ready || query.isLoading) {
    return (
      <div
        role="status"
        className="mb-4 rounded-xl border border-[#e4d9ef] bg-white px-4 py-3 text-sm text-[#6b607e]"
      >
        正在读取本机能力…
      </div>
    )
  }
  if (!workspace.owner) {
    return (
      <div
        role="status"
        className="mb-4 rounded-xl border border-[#e4d9ef] bg-white px-4 py-3 text-sm text-[#6b607e]"
      >
        登录后可核对当前账号可用的 Research 能力。
      </div>
    )
  }
  if (query.isError || !query.data) {
    return (
      <div
        role="alert"
        className="mb-4 flex items-center justify-between gap-3 rounded-xl border border-[#e8c7cf] bg-[#fff7f8] px-4 py-3 text-sm text-[#8f2e47]"
      >
        <span>暂时无法读取本机能力，研究操作不会被误报为可用。</span>
        <button
          type="button"
          onClick={() => void query.refetch()}
          className="inline-flex items-center gap-1 rounded px-2 py-1 font-medium"
        >
          <RefreshCw size={14} />重试
        </button>
      </div>
    )
  }

  const affected = query.data.capabilities.filter(item => item.required && !item.verified)
  if (affected.length === 0 && compact) {
    return (
      <div role="status" className="mb-3 inline-flex items-center gap-2 text-sm text-emerald-700">
        <CheckCircle2 size={16} />所需能力已经验证
      </div>
    )
  }

  return (
    <section
      aria-label="本机研究能力"
      data-generation={query.data.generation}
      className="mb-5 rounded-xl border border-[#e4d9ef] bg-[#fbf9fe] px-4 py-3 text-sm text-[#5d506e]"
    >
      <div className="flex items-center justify-between gap-3">
        <strong className="text-[#3d3154]">本机能力</strong>
        <span>{affected.length ? `${affected.length} 项必需能力尚未验证` : "所需能力已经验证"}</span>
      </div>
      {!compact && (
        <ul className="mt-3 grid gap-2 sm:grid-cols-2">
          {query.data.capabilities.map(item => {
            const state = capabilityState(item)
            return (
              <li key={item.id} className="rounded-lg bg-white px-3 py-2">
                <div className="flex items-center gap-2 font-medium">
                  {state === "verified" ? (
                    <CheckCircle2 size={15} className="text-emerald-600" />
                  ) : state === "missing" ? (
                    <AlertTriangle size={15} className="text-amber-600" />
                  ) : state === "unreachable" ? (
                    <WifiOff size={15} className="text-rose-600" />
                  ) : (
                    <CircleHelp size={15} className="text-slate-500" />
                  )}
                  {item.label}
                </div>
                <p className="mt-1 text-xs text-[#766b82]">
                  {capabilityLabel(state)}
                  {item.message ? ` · ${item.message}` : ""}
                </p>
                {item.recovery && (
                  <p className="mt-1 text-xs text-[#5f3c92]">{item.recovery}</p>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

type Capability = DesktopCapabilities["capabilities"][number]
type CapabilityState = "verified" | "missing" | "unreachable" | "unknown"

export function capabilityState(item: Capability): CapabilityState {
  if (item.verified) return "verified"
  if (!item.configured) return "missing"
  const reason = (item.reasonCode ?? "").toLowerCase()
  if (
    reason.includes("unreachable") ||
    reason.includes("connection") ||
    reason.includes("timeout") ||
    reason.includes("dns")
  ) {
    return "unreachable"
  }
  return "unknown"
}

function capabilityLabel(state: CapabilityState): string {
  if (state === "verified") return "已完成真实验证"
  if (state === "missing") return "缺少配置"
  if (state === "unreachable") return "真实探测确认当前不可达"
  return "尚未完成真实验证"
}
