import Link from "next/link"
import { Badge } from "@/components/common/badge"
import { CredibilityBadge } from "@/components/common/credibility-badge"
import { HeatScoreBadge } from "@/components/common/heat-score-badge"
import { QualityBadge } from "@/components/common/quality-badge"
import { SourceBadge } from "@/components/common/source-badge"
import { formatDateTime } from "@/lib/format"
import type { NewsItem } from "@/types/news"

export function NewsTable({ items }: { items: NewsItem[] }) {
  return (
    <div className="overflow-x-auto rounded-md border border-[#dbe3dc] bg-white dark:border-border dark:bg-card">
      <table className="w-full min-w-[980px] table-fixed text-left text-sm">
        <thead className="border-b border-[#d7dfd8] text-xs uppercase text-[#334155]/55 dark:border-border dark:text-muted-foreground">
          <tr>
            <th className="w-[34%] px-4 py-3 font-medium">Title</th>
            <th className="w-[15%] px-4 py-3 font-medium">Source</th>
            <th className="w-[12%] px-4 py-3 font-medium">Category</th>
            <th className="w-[10%] px-4 py-3 font-medium">Heat</th>
            <th className="w-[11%] px-4 py-3 font-medium">Quality</th>
            <th className="w-[12%] px-4 py-3 font-medium">Trust</th>
            <th className="w-[14%] px-4 py-3 font-medium">Published</th>
            <th className="w-[16%] px-4 py-3 font-medium">Topic</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id} className="border-b border-[#d7dfd8] last:border-0 dark:border-border">
              <td className="px-4 py-3">
                <Link href={`/news/${item.id}`} className="line-clamp-2 font-medium text-[#334155] hover:text-emerald-700 dark:text-foreground">
                  {item.title}
                </Link>
              </td>
              <td className="px-4 py-3">
                <SourceBadge name={item.sourceName} type={item.sourceType} />
              </td>
              <td className="px-4 py-3 text-[#334155]/65 dark:text-muted-foreground">{item.category}</td>
              <td className="px-4 py-3">
                {typeof item.heatScore === "number" ? <HeatScoreBadge value={item.heatScore} /> : <Badge tone="neutral">N/A</Badge>}
              </td>
              <td className="px-4 py-3">
                {typeof item.qualityScore === "number" ? <QualityBadge value={item.qualityScore} /> : <Badge tone="neutral">N/A</Badge>}
              </td>
              <td className="px-4 py-3">
                <CredibilityBadge value={item.credibility} />
              </td>
              <td className="px-4 py-3 text-[#334155]/65 dark:text-muted-foreground">{formatDateTime(item.publishedAt)}</td>
              <td className="px-4 py-3">
                {item.topicId && item.topicName ? (
                  <Link href={`/topics/${item.topicId}`} className="text-emerald-700 hover:text-[#334155] dark:text-link dark:hover:text-foreground">
                    {item.topicName}
                  </Link>
                ) : (
                  <span className="text-[#334155]/50 dark:text-muted-foreground">Unclustered</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
