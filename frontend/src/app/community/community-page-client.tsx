"use client"

import { useMemo } from "react"
import { useQuery } from "@tanstack/react-query"
import { useRouter, useSearchParams } from "next/navigation"
import { EmptyState } from "@/components/common/empty-state"
import { ErrorState } from "@/components/common/error-state"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { CommunityPulsePage } from "@/features/community/components/community-pulse-page"
import { fetchCommunitySignal, fetchCommunitySignals } from "@/lib/community/api"
import {
  communitySignalFiltersFromSearchParams,
  communitySignalFiltersToSearchParams,
  updateCommunitySignalFilters
} from "@/lib/community/community-signals"
import type { CommunitySignalListParams } from "@/types/community"

export function CommunityPageClient() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const selectedSignalId = searchParams.get("signal") ?? undefined
  const sourceQuestion = searchParams.get("question") ?? ""
  const filters = useMemo(
    () => communitySignalFiltersFromSearchParams(new URLSearchParams(searchParams.toString())),
    [searchParams]
  )
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["community", "signals", filters],
    queryFn: () => fetchCommunitySignals(filters)
  })
  const { data: selectedSignal } = useQuery({
    queryKey: ["community", "signal", selectedSignalId],
    queryFn: () => fetchCommunitySignal(selectedSignalId ?? ""),
    enabled: Boolean(selectedSignalId)
  })

  const setFilters = (patch: Partial<CommunitySignalListParams>) => {
    const next = updateCommunitySignalFilters(filters, patch)
    const params = preserveEntryParams(searchParams, communitySignalFiltersToSearchParams(next))
    if (selectedSignalId) params.set("signal", selectedSignalId)
    router.replace(params.size ? `/community?${params.toString()}` : "/community", { scroll: false })
  }

  const openSignal = (signalId: string) => {
    const params = preserveEntryParams(searchParams, communitySignalFiltersToSearchParams(filters))
    params.set("signal", signalId)
    router.replace(`/community?${params.toString()}`, { scroll: false })
  }

  const closeSignal = () => {
    const params = preserveEntryParams(searchParams, communitySignalFiltersToSearchParams(filters))
    router.replace(params.size ? `/community?${params.toString()}` : "/community", { scroll: false })
  }

  if (isLoading) return <PageSkeleton />

  if (isError) {
    return <ErrorState message={error instanceof Error ? error.message : "Community Pulse failed to load."} onRetry={() => refetch()} />
  }

  if (!data) {
    return <EmptyState title="No community data" description="Community Pulse data is currently unavailable." />
  }

  return (
    <CommunityPulsePage
      result={data}
      filters={filters}
      sourceQuestion={sourceQuestion}
      selectedSignal={selectedSignal}
      onChange={setFilters}
      onOpenSignal={openSignal}
      onCloseSignal={closeSignal}
    />
  )
}

export function preserveEntryParams(current: { get(name: string): string | null }, next: URLSearchParams): URLSearchParams {
  for (const key of ["question", "entry"]) {
    const value = current.get(key)
    if (value) next.set(key, value)
  }
  return next
}
