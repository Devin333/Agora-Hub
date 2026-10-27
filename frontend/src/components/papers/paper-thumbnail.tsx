import { FileSearch } from "lucide-react"
import { PdfPageThumbnail } from "@/components/papers/pdf-page-thumbnail"
import { translate } from "@/lib/i18n"
import { paperPdfUrl } from "@/lib/papers/format"
import type { Locale, Paper } from "@/lib/papers/types"

export function PaperThumbnail({
  paper,
  locale,
  renderPdfPreview = true
}: {
  paper: Paper
  locale: Locale
  renderPdfPreview?: boolean
}) {
  const pdfUrl = paperPdfUrl(paper)

  if (paper.thumbnailUrl) {
    return (
      <div
        aria-hidden="true"
        className="h-44 w-32 rounded-sm bg-contain bg-center bg-no-repeat ring-1 ring-foreground/10"
        style={{ backgroundImage: `url(${paper.thumbnailUrl})` }}
      />
    )
  }

  if (pdfUrl && renderPdfPreview) {
    return (
      <PdfPageThumbnail
        pdfUrl={pdfUrl}
        title={paper.title}
        locale={locale}
        className="h-44 w-32 shrink-0 overflow-hidden rounded-sm bg-white ring-1 ring-foreground/10 transition-transform group-hover:-translate-y-0.5"
      />
    )
  }

  if (pdfUrl) {
    return <PaperPlaceholder label={translate(locale, "papers.reader.pdfPreviewDeferred")} />
  }

  return <PaperPlaceholder label={translate(locale, "papers.reader.noVerifiedPdf")} />
}

function PaperPlaceholder({ label }: { label: string }) {
  return (
    <div className="relative h-44 w-32 shrink-0 overflow-hidden rounded-sm bg-card ring-1 ring-foreground/10">
      <div className="absolute inset-x-0 top-0 h-8 bg-muted" />
      <div className="flex h-full flex-col px-4 pb-4 pt-12">
        <FileSearch className="size-7 text-faint-foreground" strokeWidth={1.8} aria-hidden="true" />
        <div className="mt-5 space-y-2">
          <span className="block h-1.5 w-20 rounded-full bg-muted" />
          <span className="block h-1.5 w-24 rounded-full bg-muted" />
          <span className="block h-1.5 w-16 rounded-full bg-muted" />
        </div>
        <p className="mt-auto text-caption font-semibold uppercase tracking-wider text-faint-foreground">
          {label}
        </p>
      </div>
    </div>
  )
}
