"use client"

import { PaperDocumentReaderPage } from "@/components/papers/paper-reader"
import type { PaperDocumentResponse } from "@/lib/paper-reader/types"
import { useUiStore } from "@/stores/ui-store"

export function PaperDocumentReaderPageClient({ payload, backHref }: { payload: PaperDocumentResponse; backHref?: string }) {
  const locale = useUiStore((state) => state.locale)

  return <PaperDocumentReaderPage payload={payload} locale={locale} backHref={backHref} />
}
