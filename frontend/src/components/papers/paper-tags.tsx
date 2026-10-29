import Link from "next/link"
import { Badge } from "@/components/ui/badge"
import { methodName, taskName } from "@/lib/papers/format"
import { papersRoutes } from "@/lib/papers/routes"
import type { Locale, MethodRef, TaskRef } from "@/lib/papers/types"

export function PaperTags({
  tasks,
  methods,
  tags,
  locale
}: {
  tasks: TaskRef[]
  methods: MethodRef[]
  tags: string[]
  locale: Locale
}) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {tasks.slice(0, 2).map((task) => (
        <Link key={task.id} href={papersRoutes.taskDetail(task.slug)} className="rounded-full outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
          <Badge variant="success" className="hover:bg-success/25">{taskName(task, locale)}</Badge>
        </Link>
      ))}
      {methods.slice(0, 2).map((method) => (
        <Link key={method.id} href={papersRoutes.methodDetail(method.slug)} className="rounded-full outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
          <Badge variant="info" className="hover:bg-link/25">{methodName(method, locale)}</Badge>
        </Link>
      ))}
      {tags.slice(0, 2).map((tag) => (
        <Badge key={tag} variant="muted">
          {tag}
        </Badge>
      ))}
    </div>
  )
}
