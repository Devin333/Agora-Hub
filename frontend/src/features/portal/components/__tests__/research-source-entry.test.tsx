import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { ResearchSourceEntry } from "../research-source-entry"
import { acceptAccountHistory, readResearchWorkspace, selectHistoryOwner } from "@/lib/research/history"
import { emptyWorkspace } from "@/lib/research/history-model"

const success = (data: unknown) => new Response(JSON.stringify({ success: true, data }))
beforeEach(() => {
  sessionStorage.clear(); selectHistoryOwner("alice"); acceptAccountHistory(emptyWorkspace(), 0)
})
afterEach(() => { cleanup(); selectHistoryOwner(null); vi.unstubAllGlobals() })

it("retains the received identity before conversion and resumes without reuploading", async () => {
  let finish: (response: Response) => void = () => {}
  const record = { importId: "imp_test", status: "received", filename: "研究.pdf" }
  const attach = vi.fn()
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(success(record)).mockImplementationOnce(() => new Promise(resolve => { finish = resolve })))
  const view = render(<ResearchSourceEntry onAttach={attach} />)
  fireEvent.change(screen.getByLabelText("上传研究 PDF"), { target: { files: [new File(["%PDF-1.7"], "研究.pdf", { type: "application/pdf" })] } })
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
  expect(JSON.parse(sessionStorage.getItem("agora-pdf-import:alice")!)).toEqual(record)
  expect(vi.mocked(fetch).mock.calls[0][1]?.headers).toMatchObject({ "X-Filename": encodeURIComponent("研究.pdf") })
  view.unmount()
  vi.mocked(fetch).mockResolvedValue(success({ ...record, status: "completed", paperId: "owned-paper" }))
  render(<ResearchSourceEntry onAttach={attach} />)
  fireEvent.click(screen.getByRole("button", { name: "继续转换" }))
  fireEvent.click(await screen.findByRole("button", { name: "用于本次研究" }))
  expect(attach).toHaveBeenCalledWith("imp_test")
  expect(readResearchWorkspace().materials?.[0]).toMatchObject({ referenceId: "owned-paper", kind: "pdf", groupId: null })
  await act(async () => finish(success({ ...record, status: "failed" })))
  expect(screen.getByText("文本已转换，可用于本次研究")).toBeVisible()
  expect(screen.queryByRole("link", { name: "打开阅读器" })).not.toBeInTheDocument()
  expect(readResearchWorkspace().materials?.[0].readerHref).toBeUndefined()
  expect(vi.mocked(fetch).mock.calls.filter(call => call[0] === "/api/research/imports")).toHaveLength(1)
})

it("never renders an old owner's conversion response after switching accounts", async () => {
  let finish: (response: Response) => void = () => {}
  vi.stubGlobal("fetch", vi.fn(() => new Promise(resolve => { finish = resolve })))
  render(<ResearchSourceEntry />)
  fireEvent.change(screen.getByLabelText("上传研究 PDF"), { target: { files: [new File(["%PDF"], "私人资料.pdf")] } })
  act(() => { selectHistoryOwner("bob"); acceptAccountHistory(emptyWorkspace(), 0) })
  await act(async () => finish(success({ importId: "imp_private", filename: "私人资料.pdf", status: "completed", paperId: "private" })))
  expect(screen.queryByText("私人资料.pdf")).not.toBeInTheDocument()
  expect(sessionStorage.getItem("agora-pdf-import:bob")).toBeNull()
})
