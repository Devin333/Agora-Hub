import { RelatedListPanel } from "@/components/papers/shared/related-list-panel"
import { papersCopy, t } from "@/lib/papers/copy"
import { formatWholeNumber, methodName } from "@/lib/papers/format"
import { papersRoutes } from "@/lib/papers/routes"
import type { Locale, MethodRef, Paper } from "@/lib/papers/types"

export function CommonMethodsPanel({
  methods,
  papers,
  locale
}: {
  methods: MethodRef[]
  papers: Paper[]
  locale: Locale
}) {
  const items = methods
    .map((method) => ({
      method,
      count: papers.filter(
        (p) => p.isPublished !== false && (p.methodRefs ?? []).some((r) => r.slug === method.slug)
      ).length
    }))
    .filter((item) => item.count > 0)
    .map(({ method, count }) => ({
      id: method.id,
      label: methodName(method, locale),
      href: papersRoutes.methodDetail(method.slug),
      count: formatWholeNumber(count, locale)
    }))

  return <RelatedListPanel title={t(papersCopy.commonMethods, locale)} items={items} />
}
