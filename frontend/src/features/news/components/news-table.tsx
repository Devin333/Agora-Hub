import Link from "next/link"
import { Badge } from "@/components/common/badge"
import { CredibilityBadge } from "@/components/common/credibility-badge"
import { HeatScoreBadge } from "@/components/common/heat-score-badge"
import { QualityBadge } from "@/components/common/quality-badge"
import { SourceBadge } from "@/components/common/source-badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { formatDateTime } from "@/lib/format"
import type { NewsItem } from "@/types/news"

// Caption size lives on <TableHeader> so it never shares a cn() merge with the color class.
const headClass = "px-4 font-medium text-muted-foreground"

export function NewsTable({ items }: { items: NewsItem[] }) {
  return (
    <div className="overflow-hidden rounded-xl border border-border">
      <Table className="min-w-[980px] table-fixed">
        <TableHeader className="text-label">
          <TableRow className="hover:bg-transparent">
            <TableHead className={`w-[34%] ${headClass}`}>Title</TableHead>
            <TableHead className={`w-[15%] ${headClass}`}>Source</TableHead>
            <TableHead className={`w-[12%] ${headClass}`}>Category</TableHead>
            <TableHead className={`w-[10%] ${headClass}`}>Heat</TableHead>
            <TableHead className={`w-[11%] ${headClass}`}>Quality</TableHead>
            <TableHead className={`w-[12%] ${headClass}`}>Trust</TableHead>
            <TableHead className={`w-[14%] ${headClass}`}>Published</TableHead>
            <TableHead className={`w-[16%] ${headClass}`}>Topic</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody className="text-body2">
          {items.map((item) => (
            <TableRow key={item.id}>
              <TableCell className="px-4 py-3">
                <Link href={`/news/${item.id}`} className="line-clamp-2 font-medium text-foreground hover:underline">
                  {item.title}
                </Link>
              </TableCell>
              <TableCell className="px-4 py-3">
                <SourceBadge name={item.sourceName} type={item.sourceType} />
              </TableCell>
              <TableCell className="px-4 py-3 text-muted-foreground">{item.category}</TableCell>
              <TableCell className="px-4 py-3">
                {typeof item.heatScore === "number" ? <HeatScoreBadge value={item.heatScore} /> : <Badge tone="neutral">N/A</Badge>}
              </TableCell>
              <TableCell className="px-4 py-3">
                {typeof item.qualityScore === "number" ? <QualityBadge value={item.qualityScore} /> : <Badge tone="neutral">N/A</Badge>}
              </TableCell>
              <TableCell className="px-4 py-3">
                <CredibilityBadge value={item.credibility} />
              </TableCell>
              <TableCell className="px-4 py-3 text-muted-foreground">{formatDateTime(item.publishedAt)}</TableCell>
              <TableCell className="px-4 py-3">
                {item.topicId && item.topicName ? (
                  <Link href={`/topics/${item.topicId}`} className="text-link hover:underline">
                    {item.topicName}
                  </Link>
                ) : (
                  <span className="text-faint-foreground">Unclustered</span>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
