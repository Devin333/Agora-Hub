import { Suspense } from "react"
import { redirect } from "next/navigation"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { NewsPageClient } from "@/app/news/news-page-client"

export default async function NewsPage(
  props: {
    searchParams?: Promise<Record<string, string | string[] | undefined>>
  }
) {
  const searchParams = await props.searchParams;
  if (firstParam(searchParams?.source) === "community") {
    const params = new URLSearchParams()
    for (const key of ["q", "topic", "sentiment", "period", "sort", "limit", "cursor"]) {
      const value = firstParam(searchParams?.[key])
      if (value) params.set(key, value)
    }
    redirect(params.size ? `/community?${params.toString()}` : "/community")
  }

  return (
    <Suspense fallback={<PageSkeleton />}>
      <NewsPageClient />
    </Suspense>
  )
}

function firstParam(value: string | string[] | undefined) {
  return Array.isArray(value) ? value[0] : value
}
