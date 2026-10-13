"use client";
import { use } from "react";

import Link from "next/link"
import { useQuery } from "@tanstack/react-query"
import { EmptyState } from "@/components/common/empty-state"
import { ErrorState } from "@/components/common/error-state"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { CommunityTopicDetail } from "@/features/community/components/community-topic-detail"
import { fetchCommunityTopic } from "@/lib/community/api"

export default function CommunityTopicPage(props: { params: Promise<{ slug: string }> }) {
  const params = use(props.params);
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["community", "topic", params.slug],
    queryFn: () => fetchCommunityTopic(params.slug)
  })

  if (isLoading) return <PageSkeleton />

  if (isError && typeof error === "object" && error !== null && "code" in error && error.code === "community_topic_not_found") {
    return (
      <EmptyState
        title="未找到话题"
        description="当前公开 artifact 中没有这个社区脉搏话题。"
        action={
          <Link href="/community" className="rounded-md border border-border px-3 py-2 text-sm text-foreground hover:bg-secondary">
            返回社区脉搏
          </Link>
        }
      />
    )
  }

  if (isError) {
    return (
      <ErrorState
        message={error instanceof Error ? error.message : "社区话题加载失败。"}
        onRetry={() => refetch()}
      />
    )
  }

  if (!data) {
    return (
      <EmptyState
        title="未找到话题"
        description="当前公开 artifact 中没有这个社区脉搏话题。"
        action={
          <Link href="/community" className="rounded-md border border-border px-3 py-2 text-sm text-foreground hover:bg-secondary">
            返回社区脉搏
          </Link>
        }
      />
    )
  }

  return <CommunityTopicDetail topic={data} />
}
