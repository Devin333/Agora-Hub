import { CredibilityBadge, SourceBadge } from "@/components/common/badges";
import { SectionPanel } from "@/components/common/section-panel";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDate } from "@/lib/format";
import type { TopicSourceCoverage } from "@/types/topic";

const columns = [
  { label: "来源", width: "w-52" },
  { label: "条目", width: "w-24" },
  { label: "首次出现", width: "w-36" },
  { label: "更新于", width: "w-36" },
  { label: "可信度", width: "w-40" },
  { label: "覆盖说明", width: "" },
];

const cellClass = "whitespace-normal px-4 py-2.5";

export function SourceMatrix({ sources }: { sources: TopicSourceCoverage[] }) {
  return (
    <SectionPanel title="来源矩阵" framed={false}>
      {sources.length ? (
        <div className="rk-scroll overflow-x-auto rounded-xl border border-border">
          <Table className="min-w-[720px] table-fixed">
            <TableHeader className="text-label">
              <TableRow className="hover:bg-transparent">
                {columns.map((column) => (
                  <TableHead key={column.label} className={`${column.width} px-4 font-medium text-muted-foreground`}>
                    {column.label}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody className="text-body2">
              {sources.map((source) => (
                <TableRow key={source.sourceName} className="hover:bg-accent/40">
                  <TableCell className={cellClass}>
                    <SourceBadge sourceName={source.sourceName} sourceType={source.sourceType} />
                  </TableCell>
                  <TableCell className={`${cellClass} tabular-nums text-muted-foreground`}>{source.itemCount}</TableCell>
                  <TableCell className={`${cellClass} tabular-nums text-muted-foreground`}>{formatDate(source.firstSeenAt)}</TableCell>
                  <TableCell className={`${cellClass} tabular-nums text-muted-foreground`}>{formatDate(source.lastSeenAt)}</TableCell>
                  <TableCell className={cellClass}>
                    <CredibilityBadge credibility={source.credibility} />
                  </TableCell>
                  <TableCell className={`${cellClass} text-muted-foreground`}>{source.coverageSummary}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      ) : (
        <p className="py-6 text-center text-body2 text-muted-foreground">暂无来源覆盖数据。</p>
      )}
    </SectionPanel>
  );
}
