export type TocItem = {
  id: string;
  title: string;
  depth: number;
};

export function extractToc(markdown: string): TocItem[] {
  return markdown
    .split("\n")
    .filter((line) => /^#{1,3}\s/.test(line))
    .map((line) => {
      const depth = line.match(/^#+/)?.[0].length ?? 1;
      const title = line.replace(/^#+\s*/, "").trim();
      return {
        id: title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""),
        title,
        depth,
      };
    });
}

export function ReportToc({ markdown }: { markdown: string }) {
  const items = extractToc(markdown);
  return (
    <section className="space-y-3">
      <h2 className="text-body1 font-semibold text-foreground">目录</h2>
      <div className="space-y-2 rounded-xl border px-4 py-3">
        {items.map((item) => (
          <p key={item.id} className="text-body2 text-muted-foreground" style={{ paddingLeft: `${(item.depth - 1) * 12}px` }}>
            {item.title}
          </p>
        ))}
      </div>
    </section>
  );
}
