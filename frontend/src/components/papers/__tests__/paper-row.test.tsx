import { fireEvent, render, screen } from "@testing-library/react"
import { renderToString } from "react-dom/server"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { PaperRow } from "@/components/papers/paper-row"
import type { Paper } from "@/lib/papers/types"
import { usePaperWorkspaceStore } from "@/stores/paper-workspace-store"
import { useUiStore } from "@/stores/ui-store"

const paper: Paper = {
  id: "paper-swe-agent",
  slug: "swe-agent-agent-computer-interfaces",
  title: "SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering",
  abstractSnippet: "Language model agents can automate complex software engineering tasks.",
  authors: ["John Yang", "Carlos E. Jimenez"],
  publishedAt: "2024-05-06",
  venue: "arXiv",
  citationCount: 24,
  tags: ["agents"],
  taskRefs: [],
  methodRefs: [],
  githubStars: 19281,
  pdfUrl: "https://arxiv.org/pdf/2405.15793.pdf",
  repoUrl: "https://github.com/SWE-agent/SWE-agent",
  isPublished: true
}

describe("PaperRow", () => {
  beforeEach(() => {
    useUiStore.setState({ locale: "en" })
    usePaperWorkspaceStore.getState().clear()
  })

  it("links PDF and GitHub actions to real destinations and avoids stars per hour copy", () => {
    render(<PaperRow paper={paper} locale="en" onPreview={vi.fn()} />)

    expect(screen.getByRole("link", { name: paper.title })).toHaveAttribute("href", "/papers/swe-agent-agent-computer-interfaces")
    expect(screen.getAllByRole("link", { name: /open paper pdf/i })[0]).toHaveAttribute("href", "https://arxiv.org/pdf/2405.15793.pdf")
    expect(screen.getAllByRole("link", { name: /open github repository/i })[0]).toHaveAttribute("href", "https://github.com/SWE-agent/SWE-agent")
    expect(screen.getByText(/24 Cites/i)).toBeInTheDocument()
    expect(screen.getByText(/19.3K Stars/i)).toBeInTheDocument()
    expect(screen.getAllByRole("button", { name: /preview/i })).not.toHaveLength(0)
    expect(screen.getByRole("link", { name: /read/i })).toHaveAttribute("href", "/papers/swe-agent-agent-computer-interfaces/read")
    expect(screen.queryByText(/stars \/ hr/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/github momentum/i)).not.toBeInTheDocument()
  })

  it("hides unavailable citation metrics instead of rendering N/A", () => {
    render(<PaperRow paper={{ ...paper, citationCount: undefined }} locale="en" onPreview={vi.fn()} />)

    expect(screen.queryByText("N/A")).not.toBeInTheDocument()
    expect(screen.queryByText("Cites")).not.toBeInTheDocument()
  })

  it("renders a repeated source tag only once", () => {
    render(<PaperRow paper={{ ...paper, tags: ["agents", "agents"] }} locale="en" onPreview={vi.fn()} />)
    expect(screen.getAllByText("agents", { exact: true })).toHaveLength(1)
  })

  it("uses the server snapshot before hydrating persisted workspace lists", () => {
    usePaperWorkspaceStore.setState({ readingList: [paper.id], compare: [paper.id], later: [paper.id] })
    const html = renderToString(<PaperRow paper={paper} locale="en" onPreview={vi.fn()} renderPdfPreview={false} />)
    expect(html).toContain("Add to reading list")
    expect(html).toContain("Add to compare")
    expect(html).not.toContain('aria-pressed="true"')
  })

  it("uses product typography for paper metadata and abstract copy", () => {
    render(<PaperRow paper={paper} locale="en" onPreview={vi.fn()} />)

    expect(screen.getByText(/John Yang/).closest("p")).not.toHaveAttribute("style")
    expect(screen.getByText(paper.abstractSnippet).closest("p")).not.toHaveAttribute("style")
    expect(screen.getByText(paper.abstractSnippet).closest("p")).toHaveClass("line-clamp-3")
  })

  it("uses product typography for the paper title", () => {
    render(<PaperRow paper={paper} locale="en" onPreview={vi.fn()} />)

    expect(screen.getByRole("heading", { name: paper.title })).not.toHaveAttribute("style")
  })

  it("does not render a GitHub root link", () => {
    render(<PaperRow paper={{ ...paper, repoUrl: "https://github.com/" }} locale="en" onPreview={vi.fn()} />)

    expect(screen.queryByRole("link", { name: /open github repository/i })).not.toBeInTheDocument()
  })

  it("shows user state badges when available", () => {
    render(
      <PaperRow
        paper={{
          ...paper,
          userState: {
            userId: "user-1",
            paperId: paper.id,
            favorite: true,
            subscribed: true,
            readingStatus: "reading",
            progressPercent: 35,
            updatedAt: "2026-05-24T00:00:00Z"
          }
        }}
        locale="en"
        onPreview={vi.fn()}
      />
    )

    expect(screen.getByText("Favorite")).toBeInTheDocument()
    expect(screen.getByText("Subscribed")).toBeInTheDocument()
  })

  it("supports local reading list, compare, and read-later workspace actions", () => {
    render(<PaperRow paper={paper} locale="en" onPreview={vi.fn()} />)

    const readingList = screen.getByRole("button", { name: /add to reading list/i })
    const compare = screen.getByRole("button", { name: /add to compare/i })
    const later = screen.getByRole("button", { name: /read later/i })

    fireEvent.click(readingList)
    fireEvent.click(compare)
    fireEvent.click(later)

    expect(screen.getByRole("button", { name: /in reading list/i })).toHaveAttribute("aria-pressed", "true")
    expect(screen.getByRole("button", { name: /in compare/i })).toHaveAttribute("aria-pressed", "true")
    expect(screen.getByRole("button", { name: /saved for later/i })).toHaveAttribute("aria-pressed", "true")
    expect(usePaperWorkspaceStore.getState().readingList).toContain(paper.id)
    expect(usePaperWorkspaceStore.getState().compare).toContain(paper.id)
    expect(usePaperWorkspaceStore.getState().later).toContain(paper.id)
  })
})
