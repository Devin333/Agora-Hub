"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowDownUp, ChevronDown } from "lucide-react";
import { Badge } from "@/components/common/badges";
import { EmptyState } from "@/components/common/empty-state";
import { SectionPanel } from "@/components/common/section-panel";
import { Button } from "@/components/ui/button";
import { evidences } from "@/lib/mock-data";
import { formatDateTime } from "@/lib/format";
import type { TopicTimelineItem } from "@/types/topic";

const importanceTone = { high: "good", medium: "info", low: "neutral" } as const;

export function TopicTimeline({ items }: { items: TopicTimelineItem[] }) {
  const [expanded, setExpanded] = useState<string>();
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const sorted = [...items].sort((a, b) => {
    const diff = new Date(a.occurredAt).getTime() - new Date(b.occurredAt).getTime();
    return order === "asc" ? diff : -diff;
  });

  if (!items.length) {
    return <EmptyState title="暂无时间线" description="这个主题还没有积累足够的事件历史。" />;
  }

  return (
    <SectionPanel
      title="时间线"
      framed={false}
      actions={
        <Button variant="outline" size="sm" onClick={() => setOrder(order === "asc" ? "desc" : "asc")}>
          <ArrowDownUp size={15} strokeWidth={1.8} aria-hidden="true" />
          {order === "asc" ? "最早优先" : "最新优先"}
        </Button>
      }
    >
      <ol className="divide-y divide-border overflow-hidden rounded-xl border border-border">
        {sorted.map((item) => {
          const open = expanded === item.id;
          const panelId = `timeline-${item.id}-details`;
          const relatedEvidence = evidences.filter((evidence) => item.evidenceIds.includes(evidence.id));
          return (
            <li key={item.id}>
              <button
                type="button"
                aria-expanded={open}
                aria-controls={panelId}
                className="block w-full px-4 py-3 text-left outline-none transition-colors hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-inset focus-visible:ring-ring/50"
                onClick={() => setExpanded(open ? undefined : item.id)}
              >
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex min-w-0 items-start gap-2">
                    <ChevronDown
                      size={15}
                      strokeWidth={1.8}
                      aria-hidden="true"
                      className={`mt-0.5 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-180" : ""}`}
                    />
                    <div className="min-w-0">
                      <p className="text-body2 font-semibold text-foreground">{item.title}</p>
                      <p className="mt-0.5 text-label tabular-nums text-muted-foreground">
                        {formatDateTime(item.occurredAt)} · {item.sourceCount} 个来源
                      </p>
                    </div>
                  </div>
                  <div className="flex gap-1.5">
                    <Badge tone={importanceTone[item.importance] ?? "neutral"}>{item.importance}</Badge>
                    <Badge>{item.type}</Badge>
                  </div>
                </div>
                <p className="mt-2 pl-6 text-[13px] leading-relaxed text-muted-foreground">{item.summary}</p>
              </button>
              {open ? (
                <div id={panelId} className="space-y-2 px-4 pb-3 pl-10">
                  {relatedEvidence.map((evidence) => (
                    <p key={evidence.id} className="rounded-lg bg-muted px-3 py-2 text-label text-muted-foreground">
                      <span className="font-medium text-foreground">{evidence.title}</span>: {evidence.summary}
                    </p>
                  ))}
                  {item.relatedNewsId ? (
                    <Link
                      className="inline-block rounded-sm text-body2 font-medium text-link outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"
                      href={`/search?type=news&q=${encodeURIComponent(item.relatedNewsId)}`}
                    >
                      打开相关新闻
                    </Link>
                  ) : null}
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
    </SectionPanel>
  );
}
