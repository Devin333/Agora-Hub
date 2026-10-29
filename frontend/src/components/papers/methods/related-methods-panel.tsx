import { RelatedListPanel } from "@/components/papers/shared/related-list-panel"
import { papersCopy, t } from "@/lib/papers/copy"
import { methodName } from "@/lib/papers/format"
import { papersRoutes } from "@/lib/papers/routes"
import type { Locale, MethodRef } from "@/lib/papers/types"

export function RelatedMethodsPanel({ methods, locale }: { methods: MethodRef[]; locale: Locale }) {
  const items = methods.map((method) => ({
    id: method.id,
    label: methodName(method, locale),
    href: papersRoutes.methodDetail(method.slug)
  }))

  return <RelatedListPanel title={t(papersCopy.relatedMethods, locale)} items={items} />
}
