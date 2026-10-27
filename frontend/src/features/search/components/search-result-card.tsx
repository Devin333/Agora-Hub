import Link from "next/link";
import { Badge } from "@/components/common/badges";
import { formatDateTime, titleCase } from "@/lib/format";
import type { SearchResult } from "@/types/search";

/** One search hit rendered as a row inside the divided result list. */
export function SearchResultCard({ result }: { result: SearchResult }) {
  const external = result.href.startsWith("http");
  return (
    <Link
      href={result.href}
      target={external ? "_blank" : undefined}
      rel={external ? "noreferrer" : undefined}
      className="group block px-4 py-3 outline-none transition-colors hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-inset focus-visible:ring-ring/50"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-[15px] font-semibold text-foreground group-hover:underline">{result.title}</h2>
          <p className="mt-0.5 text-label text-muted-foreground">
            {titleCase(result.objectType)} {result.timestamp ? `· ${formatDateTime(result.timestamp)}` : null}
          </p>
        </div>
        <Badge tone="accent">相关度 {Math.round(result.relevanceScore ?? 0)}</Badge>
      </div>
      {result.summary ? <p className="mt-1.5 text-[13px] leading-relaxed text-muted-foreground">{result.summary}</p> : null}
      {result.matchedSnippet ? (
        <p className="mt-2 rounded-lg bg-muted px-3 py-2 text-label text-muted-foreground">{result.matchedSnippet}</p>
      ) : null}
      {result.tags?.length ? (
        <div className="mt-2 flex flex-wrap gap-1">
          {result.tags.map((tag) => (
            <span key={tag} className="rounded-md bg-muted px-2 py-0.5 text-label text-muted-foreground">
              #{tag}
            </span>
          ))}
        </div>
      ) : null}
    </Link>
  );
}
