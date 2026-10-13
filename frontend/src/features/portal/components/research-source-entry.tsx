"use client";
import { useEffect, useRef, useState } from "react";
import { LoaderCircle, RefreshCw } from "lucide-react";
import { usePortalAccount } from "@/components/auth/portal-account-provider";
import { historyState, saveResearchMaterial } from "@/lib/research/history";
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history";
import { fetchDesktopIdentity, fetchResearchOperation } from "@/lib/research/desktop/client";
import { operationSchema, type ResearchOperation } from "@/lib/research/desktop/contracts";
import { isLocalDesktopProfile } from "@/lib/deployment-profile";
import { operationReceiptMatches, prepareResearchOperation, updateResearchOperation, type OperationReceiptIdentity } from "@/lib/research/pending-operations";
type ImportStatus = "received" | "parsing" | "completed" | "failed" | "needs_action" | "rejected" | "passed" | "interrupted" | "unknown" | "waiting" | "running";
type ImportRecord = {
    importId?: string;
    requestId?: string;
    generation?: string;
    fingerprint?: string;
    status: ImportStatus;
    filename?: string;
    paperId?: string;
    sourceRevision?: string;
    sourceFingerprint?: string;
    error?: {
        message?: string;
    } | null;
};
function validRecord(value: unknown): value is ImportRecord {
    if (!value || typeof value !== "object")
        return false;
    const record = value as ImportRecord;
    const statuses: ImportStatus[] = ["received", "parsing", "completed", "failed", "needs_action", "rejected", "passed", "interrupted", "unknown", "waiting", "running"];
    const hasRequestIdentity = record.requestId !== undefined || record.generation !== undefined || record.fingerprint !== undefined;
    const validRequestIdentity = !hasRequestIdentity || (
        typeof record.requestId === "string" && record.requestId.length >= 8 && record.requestId.length <= 128 &&
        typeof record.generation === "string" && /^gen_[0-9a-f]{32}$/.test(record.generation) &&
        typeof record.fingerprint === "string" && /^sha256:[0-9a-f]{64}$/.test(record.fingerprint)
    );
    return statuses.includes(record.status) && validRequestIdentity && (!record.paperId || /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(record.paperId));
}
export function ResearchSourceEntry({ onAttach }: {
    onAttach?: (id: string) => void;
}) {
    const workspace = useOwnedResearchWorkspace();
    return workspace.ready ? <OwnedSourceEntry key={workspace.owner ?? "guest"} owner={workspace.owner!} onAttach={onAttach}/> : null;
}
function OwnedSourceEntry({ owner, onAttach }: {
    owner: string | null;
    onAttach?: (id: string) => void;
}) {
    const account = usePortalAccount();
    const input = useRef<HTMLInputElement>(null);
    const current = useRef(true);
    const request = useRef<AbortController | null>(null);
    const attach = useRef(onAttach);
    attach.current = onAttach;
    const storageKey = owner ? `agora-pdf-import:${owner}` : null;
    const [record, setRecord] = useState<ImportRecord | null>(null);
    const [filename, setFilename] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [attached, setAttached] = useState(false);
    const isCurrent = () => current.current && historyState().owner === owner;
    function keep(value: ImportRecord) {
        if (!isCurrent())
            return;
        setRecord(value);
        if (!storageKey)
            return;
        try {
            sessionStorage.setItem(storageKey, JSON.stringify(value));
        }
        catch {
            setError("导入请求身份无法保存在当前标签页，请不要关闭页面。");
        }
    }
    useEffect(() => {
        current.current = true;
        if (storageKey) {
            try {
                const saved = JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
                if (validRecord(saved)) {
                    setRecord(saved);
                    setFilename(saved.filename ?? "PDF");
                }
            }
            catch {
                setError("上次导入状态已损坏，请重新选择文件创建新请求。");
            }
        }
        const open = () => {
            if (request.current)
                return;
            if (!owner) {
                const trigger = document.querySelector<HTMLElement>('[aria-label="添加到本次研究"]');
                if (trigger)
                    account?.openLogin(trigger);
            }
            else
                input.current?.click();
        };
        window.addEventListener("agora-open-pdf-import", open);
        return () => { current.current = false; request.current?.abort(); window.removeEventListener("agora-open-pdf-import", open); };
    }, [account, owner, storageKey]);
    async function upload(file: File) {
        if (busy || !owner)
            return;
        setError("");
        setAttached(false);
        if (!file.name.toLowerCase().endsWith(".pdf") || (file.type && file.type !== "application/pdf")) {
            setError("请选择 PDF 文件。");
            return;
        }
        if (file.size > 50 * 1024 * 1024) {
            setError("PDF 不能超过 50 MB，请选择更小的文件。");
            return;
        }
        const controller = new AbortController();
        request.current = controller;
        setBusy(true);
        setFilename(file.name);
        try {
            const content = await file.arrayBuffer();
            if (!isLocalDesktopProfile()) {
                await uploadLegacy(file, content, controller);
                return;
            }
            await uploadDesktop(file, content, controller);
        }
        catch (cause) {
            if (isCurrent())
                setError(controller.signal.aborted ? "等待已停止；已有请求仍需核对，不会自动重复导入。" : cause instanceof Error ? cause.message : "PDF 导入结果暂时无法确认。");
        }
        finally {
            if (request.current === controller) {
                request.current = null;
                setBusy(false);
            }
        }
    }
    async function uploadLegacy(file: File, content: ArrayBuffer, controller: AbortController) {
        const response = await fetch("/api/research/imports", { method: "POST", credentials: "same-origin", signal: controller.signal, headers: { "Content-Type": "application/pdf", "X-Filename": encodeURIComponent(file.name) }, body: content });
        const payload = await response.json();
        if (!response.ok || !payload.success || !validRecord(payload.data))
            throw new Error(payload?.error?.message || "PDF 导入失败。");
        keep(payload.data);
        if (payload.data.importId)
            void checkLegacy(payload.data.importId, controller);
    }
    async function uploadDesktop(file: File, content: ArrayBuffer, controller: AbortController) {
        const sourceFingerprint = `sha256:${await sha256Hex(content)}`;
        const sourceRevision = sourceFingerprint;
        const identity = await fetchDesktopIdentity(controller.signal);
        const semantic = { kind: "import" as const, filename: file.name, sizeBytes: file.size, sourceRevision, sourceFingerprint };
        const prepared = await prepareResearchOperation({ kind: "import", generation: identity.generation, semantic, sourceRevision });
        updateResearchOperation(prepared.requestId, { status: "waiting" });
        keep({ requestId: prepared.requestId, generation: identity.generation, fingerprint: prepared.fingerprint, status: "waiting", filename: file.name, sourceRevision, sourceFingerprint });
        let response: Response;
        try {
            response = await fetch("/api/research/imports", { method: "POST", credentials: "same-origin", signal: controller.signal, headers: { "Content-Type": "application/pdf", "X-Filename": encodeURIComponent(file.name), "X-Newsroom-Request-Id": prepared.requestId, "X-Newsroom-Generation": identity.generation, "X-Newsroom-Request-Fingerprint": prepared.fingerprint, "X-Newsroom-Source-Revision": sourceRevision, "X-Newsroom-Source-Fingerprint": sourceFingerprint }, body: content });
        }
        catch (cause) {
            updateResearchOperation(prepared.requestId, { status: "unknown" });
            keep({ requestId: prepared.requestId, generation: identity.generation, fingerprint: prepared.fingerprint, status: "unknown", filename: file.name, sourceRevision, sourceFingerprint });
            throw cause;
        }
        const payload = await response.json();
        if (!response.ok || !payload.success) {
            updateResearchOperation(prepared.requestId, { status: "unknown" });
            keep({ requestId: prepared.requestId, generation: identity.generation, status: "unknown", filename: file.name, sourceRevision, sourceFingerprint });
            throw new Error(payload?.error?.message || "PDF 导入失败。");
        }
        applyOperation(payload.data, file.name, prepared);
    }
    function applyOperation(
        value: unknown,
        knownFilename?: string,
        expectedOperation?: OperationReceiptIdentity,
    ) {
        const operation = operationSchema.parse(value) as ResearchOperation;
        const expected = expectedOperation ?? (record?.requestId && record.generation && record.fingerprint
            ? {
                requestId: record.requestId,
                generation: record.generation,
                fingerprint: record.fingerprint,
                kind: "import" as const,
            }
            : null);
        if (!expected || !operationReceiptMatches(operation, expected)) {
            if (expected) updateResearchOperation(expected.requestId, { status: "unknown" });
            keep({
                ...(record ?? { status: "unknown" }),
                ...(expected ? {
                    requestId: expected.requestId,
                    generation: expected.generation,
                    fingerprint: expected.fingerprint,
                } : {}),
                status: "unknown",
                filename: knownFilename || record?.filename || filename || "PDF",
            });
            setError("导入回执与已保存的请求身份不一致，请检查原请求，不能采用该结果。");
            return;
        }
        const result = operation.result && typeof operation.result === "object" ? operation.result : {};
        keep({ requestId: operation.requestId, generation: operation.generation, fingerprint: operation.fingerprint, importId: operation.importId, status: operation.status, filename: knownFilename || record?.filename || filename || (typeof result.filename === "string" ? result.filename : undefined), paperId: typeof result.paperId === "string" ? result.paperId : operation.paperId, sourceRevision: typeof result.sourceRevision === "string" ? result.sourceRevision : operation.sourceRevision, sourceFingerprint: typeof result.sourceFingerprint === "string" ? result.sourceFingerprint : undefined, error: operation.message ? { message: operation.message } : null });
        updateResearchOperation(operation.requestId, { status: operation.status, ...(operation.importId ? { importId: operation.importId } : {}), ...(operation.errorCode ? { errorCode: operation.errorCode } : {}) });
    }
    async function checkLegacy(importId: string, existing?: AbortController) {
        const controller = existing ?? new AbortController();
        if (!existing) {
            request.current = controller;
            setBusy(true);
            setError("");
        }
        try {
            const response = await fetch(`/api/research/imports/${encodeURIComponent(importId)}`, { credentials: "same-origin", signal: controller.signal, cache: "no-store" });
            const payload = await response.json();
            if (!response.ok || !payload.success || !validRecord(payload.data))
                throw new Error(payload?.error?.message || "转换状态暂时不可用。");
            keep(payload.data);
        }
        catch (cause) {
            if (!controller.signal.aborted)
                setError(cause instanceof Error ? cause.message : "转换状态暂时不可用。");
        }
        finally {
            if (!existing && request.current === controller) {
                request.current = null;
                setBusy(false);
            }
        }
    }
    async function check() {
        if (request.current)
            return;
        if (!isLocalDesktopProfile() && record?.importId) {
            await checkLegacy(record.importId);
            return;
        }
        if (!record?.requestId || !record.generation || !record.fingerprint)
            return;
        const controller = new AbortController();
        request.current = controller;
        setBusy(true);
        setError("");
        try {
            applyOperation(await fetchResearchOperation(record.requestId, record.generation, controller.signal));
        }
        catch (cause) {
            setError(cause instanceof Error ? cause.message : "导入结果暂时无法确认。");
        }
        finally {
            request.current = null;
            setBusy(false);
        }
    }
    function addToResearch() {
        if (!isCurrent() || !record?.paperId || !record.importId || !attach.current)
            return;
        const now = Date.now();
        const readerHref = isLocalDesktopProfile() ? `/papers/${encodeURIComponent(record.paperId)}/read${record.sourceRevision ? `?sourceRevision=${encodeURIComponent(record.sourceRevision)}` : ""}` : undefined;
        const saved = saveResearchMaterial({ id: record.importId, groupId: null, kind: "pdf", title: record.filename || filename || "导入的 PDF", url: "", referenceId: record.paperId, ...(readerHref ? { readerHref } : {}), notes: "", createdAt: now, updatedAt: now }, owner);
        if (saved) {
            attach.current(record.importId);
            setAttached(true);
        }
        else
            setError(historyState().message || "暂时无法引用 PDF，请重试。");
    }
    const completed = Boolean(
        record &&
        (record.status === "completed" || record.status === "passed") &&
        record.paperId,
    );
    const readerHref = record?.paperId
        ? `/papers/${encodeURIComponent(record.paperId)}/read${record.sourceRevision
            ? `?sourceRevision=${encodeURIComponent(record.sourceRevision)}`
            : ""}`
        : "";
    return (
        <div className={record || error || busy
            ? "mt-4 flex flex-wrap items-center gap-3 text-sm"
            : "sr-only"}>
            <input
                ref={input}
                type="file"
                aria-label="上传研究 PDF"
                accept="application/pdf,.pdf"
                className="sr-only"
                onChange={event => {
                    const file = event.target.files?.[0];
                    if (file) void upload(file);
                    event.target.value = "";
                }}
            />
            {busy && (
                <span role="status" className="inline-flex items-center gap-2 text-[#6d28d9]">
                    <LoaderCircle size={15} className="animate-spin" />
                    正在核对 {filename || "PDF"} 的真实导入状态…
                </span>
            )}
            {!busy && record && (
                <span role="status" className="inline-flex flex-wrap items-center gap-3 text-[#6d28d9]">
                    <span>{record.filename || filename}</span>
                    {completed ? (
                        <>
                            <span>文本已转换，可用于本次研究</span>
                            {isLocalDesktopProfile() && readerHref && (
                                <a className="underline" href={readerHref}>打开阅读器</a>
                            )}
                            {onAttach && (
                                <button
                                    type="button"
                                    disabled={attached}
                                    className="underline"
                                    onClick={addToResearch}
                                >
                                    {attached ? "已加入本次研究" : "用于本次研究"}
                                </button>
                            )}
                        </>
                    ) : (
                        <>
                            <span>{importStatusMessage(record)}</span>
                            {["failed", "rejected", "needs_action"].includes(record.status) ? (
                                <button
                                    type="button"
                                    className="underline"
                                    onClick={() => input.current?.click()}
                                >
                                    重新选择文件并创建新请求
                                </button>
                            ) : (
                                <button
                                    type="button"
                                    className="inline-flex items-center gap-1 underline"
                                    onClick={() => void check()}
                                >
                                    <RefreshCw size={14} />
                                    {isLocalDesktopProfile() ? "检查已有请求" : "继续转换"}
                                </button>
                            )}
                        </>
                    )}
                </span>
            )}
            {error && <span role="alert" className="text-[#a02b47]">{error}</span>}
        </div>
    );
}

function importStatusMessage(record: ImportRecord): string {
    if (record.status === "failed") {
        return record.error?.message || "导入失败，请重新选择文件创建新请求";
    }
    if (record.status === "needs_action" || record.status === "rejected") {
        return "结果需要核对";
    }
    if (record.status === "unknown" || record.status === "interrupted") {
        return "导入回执尚未确认，请先检查已有请求";
    }
    return "导入已接收，等待真实处理结果";
}
async function sha256Hex(content: ArrayBuffer) {
    const digest = await crypto.subtle.digest("SHA-256", content);
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}
