import { PageHeader } from "@/components/layout/page-header"

export type PlaceholderPageProps = {
  eyebrow: string
  title: string
  description: string
  focus: string[]
}

export function PlaceholderPage({ eyebrow, title, description, focus }: PlaceholderPageProps) {
  return (
    <div className="space-y-5">
      <PageHeader eyebrow={eyebrow} title={title} description={description} />
      <section className="space-y-3">
        <h2 className="text-[15px] font-semibold text-foreground">规划中的工作区</h2>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {focus.map((item) => (
            <div key={item} className="rounded-2xl border border-border bg-card p-4 text-body2 leading-relaxed text-muted-foreground">
              {item}
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
