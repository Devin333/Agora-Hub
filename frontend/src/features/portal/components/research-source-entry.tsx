"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { LoaderCircle, RefreshCw } from "lucide-react";
import { usePortalAccount } from "@/components/auth/portal-account-provider";
import { historyState, saveResearchMaterial } from "@/lib/research/history";
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history";
import { fetchDesktopIdentity, fetchResearchOperation, submitResearchPrepare } from "@/lib/research/desktop/client";
import { operationSchema, type ResearchOperation } from "@/lib/research/desktop/contracts";
import { isLocalDesktopProfile } from "@/lib/deployment-profile";
import { mergeResearchOperationUpdate, operationReceiptMatches, operationReceiptUpdate, prepareResearchOperation, updateResearchOperation } from "@/lib/research/pending-operations";
import type { ResearchMaterial, ResearchPendingOperation } from "@/lib/research/workspace-items";
import { buildPrepareRequest, normalizeAnalyzeTransportUrl } from "@/lib/research/desktop/request-contracts";
import styles from "./research-source-entry.module.css";
type ImportStatus = "received" | "parsing" | "completed" | "failed" | "needs_action" | "rejected" | "passed" | "interrupted" | "unknown" | "waiting" | "running";
type ImportRecord = {
    updatedAt?: number;
    kind?: "import" | "prepare";
    sourceUrl?: string;
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
        code?: string;
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
export type ResearchSourceEntryAppearance = "portal" | "paper-agent";
const noOperations: ResearchPendingOperation[] = [];
const noMaterials: ResearchMaterial[] = [];

export function ResearchSourceEntry({ onAttach, appearance = "portal" }: {
    onAttach?: (id: string) => void;
    appearance?: ResearchSourceEntryAppearance;
}) {
    const workspace = useOwnedResearchWorkspace();
    return workspace.ready ? <OwnedSourceEntry key={workspace.owner ?? "guest"} owner={workspace.owner!} operations={workspace.workspace.pendingOperations ?? noOperations} materials={workspace.workspace.materials ?? noMaterials} saveConfirmed={!workspace.dirty && workspace.status === "synced"} onAttach={onAttach} appearance={appearance}/> : null;
}
function OwnedSourceEntry({ owner, operations, materials, saveConfirmed, onAttach, appearance }: {
    owner: string | null;
    operations: ResearchPendingOperation[];
    materials: ResearchMaterial[];
    saveConfirmed: boolean;
    onAttach?: (id: string) => void;
    appearance: ResearchSourceEntryAppearance;
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
    const [sourceInput, setSourceInput] = useState("");
    const [sourcePaperId, setSourcePaperId] = useState("");
    const targets = materials.filter((material, index, all) => identifiedPaper(material) && all.findIndex(item => identifiedPaper(item) && item.referenceId === material.referenceId) === index);
    const selectedTarget = targets.find(material => material.referenceId === sourcePaperId);
    const [online, setOnline] = useState(true);
    useEffect(() => {
        const update = () => setOnline(navigator.onLine);
        update(); window.addEventListener("online", update); window.addEventListener("offline", update);
        return () => { window.removeEventListener("online", update); window.removeEventListener("offline", update); };
    }, []);
    const isCurrent = useCallback(() => current.current && historyState().owner === owner, [owner]);
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
                if (validRecord(saved) && (!isLocalDesktopProfile() || !historyState().workspace.pendingOperations?.some(item => (item.kind === "import" || item.kind === "prepare")))) {
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
    useEffect(() => {
        const saved = [...operations].filter(item => (item.kind === "import" || item.kind === "prepare") && item.semantic.kind === item.kind).sort((left, right) => right.updatedAt - left.updatedAt)[0];
        if (!saved || !isCurrent()) return;
        const recovered = sourceRecordFromPending(saved);
        setRecord(previous => previous?.updatedAt && previous.updatedAt > saved.updatedAt ? previous : recovered);
        setFilename(recovered.filename ?? "PDF");
    }, [isCurrent, operations]);
    async function prepareSource() {
        if (!owner || !isCurrent() || request.current || !online) return;
        const sourceUrl = normalizeDirectSource(sourceInput);
        const paperId = sourcePaperId.trim();
        const target = historyState().workspace.materials?.find(material => identifiedPaper(material) && material.referenceId === paperId);
        if (!sourceUrl || !target) {
            setError("请选择当前论文库中已识别的目标论文，并输入对应的公开 HTTP(S) URL 或 DOI。未知来源请先在线检索、选择真实结果并保存。");
            return;
        }
        const previous = [...operations].reverse().find(item => item.semantic.kind === "prepare" && item.semantic.paperId === paperId && item.semantic.sourceUrl === sourceUrl && !["failed", "rejected", "needs_action"].includes(item.status));
        if (previous) {
            setRecord(sourceRecordFromPending(previous));
            setError("该来源已有保存的请求；请检查原请求，不会重复准备或自动重传。");
            return;
        }
        const controller = new AbortController();
        request.current = controller; setBusy(true); setError(""); setAttached(false); setFilename(sourceUrl);
        let prepared: ResearchPendingOperation | undefined;
        try {
            const identity = await fetchDesktopIdentity(controller.signal);
            if (!isCurrent() || controller.signal.aborted || identity.owner.userId !== owner) return;
            if (!historyState().workspace.materials?.some(material => identifiedPaper(material) && material.referenceId === paperId)) throw new Error("目标论文已不在当前库中，请重新选择；未提交来源准备。");
            const semantic = { kind: "prepare" as const, paperId, sourceUrl, sourceType: null };
            prepared = await prepareResearchOperation({ kind: "prepare", generation: identity.generation, semantic, paperId });
            if (!isCurrent() || controller.signal.aborted) return;
            updateResearchOperation(prepared.requestId, { status: "waiting" }, prepared, owner);
            keep(sourceRecordFromPending({ ...prepared, status: "waiting" }));
            if (!historyState().workspace.materials?.some(material => identifiedPaper(material) && material.referenceId === paperId)) throw new Error("目标论文已不在当前库中，来源准备未发送；原请求身份已保留。");
            const result = await submitResearchPrepare(buildPrepareRequest(prepared, semantic), controller.signal);
            if (isCurrent() && !controller.signal.aborted) applyOperation(result, sourceUrl, prepared);
        } catch (cause) {
            if (isCurrent()) {
                if (prepared) { updateResearchOperation(prepared.requestId, { status: "unknown" }, prepared, owner); keep(sourceRecordFromPending({ ...prepared, status: "unknown" })); }
                setError(cause instanceof Error ? cause.message : "来源准备结果暂时无法确认；请核对原请求。");
            }
        } finally {
            if (request.current === controller) { request.current = null; if (isCurrent()) setBusy(false); }
        }
    }
    async function upload(file: File) {
        if (busy || !owner || !isCurrent() || !online)
            return;
        setError("");
        setAttached(false);
        if (!file.name.toLowerCase().endsWith(".pdf") || (file.type && file.type !== "application/pdf")) {
            setError("请选择 PDF 文件。");
            return;
        }
        if (file.size === 0 || file.size > 50 * 1024 * 1024) {
            setError("PDF 不能为空且不能超过 50 MB，请选择有效文件。");
            return;
        }
        const controller = new AbortController();
        request.current = controller;
        setBusy(true);
        setFilename(file.name);
        try {
            const content = await file.arrayBuffer();
            if (!isCurrent() || controller.signal.aborted) return;
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
            throw new Error(importErrorMessage(payload?.error, "PDF 导入失败。"));
        keep(payload.data);
        if (payload.data.importId)
            void checkLegacy(payload.data.importId, controller);
    }
    async function uploadDesktop(file: File, content: ArrayBuffer, controller: AbortController) {
        const sourceFingerprint = `sha256:${await sha256Hex(content)}`;
        const sourceRevision = sourceFingerprint;
        const identity = await fetchDesktopIdentity(controller.signal);
        if (!isCurrent() || controller.signal.aborted || identity.owner.userId !== owner) throw new Error("导入 owner 已改变，未提交文件。");
        const semantic = { kind: "import" as const, filename: file.name, sizeBytes: file.size, sourceRevision, sourceFingerprint };
        const prepared = await prepareResearchOperation({ kind: "import", generation: identity.generation, semantic, sourceRevision });
        if (!isCurrent() || controller.signal.aborted) return;
        updateResearchOperation(prepared.requestId, { status: "waiting" }, prepared, owner ?? undefined);
        keep({ requestId: prepared.requestId, generation: identity.generation, fingerprint: prepared.fingerprint, status: "waiting", filename: file.name, sourceRevision, sourceFingerprint });
        let response: Response;
        try {
            response = await fetch("/api/research/imports", { method: "POST", credentials: "same-origin", signal: controller.signal, headers: { "Content-Type": "application/pdf", "X-Filename": encodeURIComponent(file.name), "X-Newsroom-Request-Id": prepared.requestId, "X-Newsroom-Generation": identity.generation, "X-Newsroom-Request-Fingerprint": prepared.fingerprint, "X-Newsroom-Source-Revision": sourceRevision, "X-Newsroom-Source-Fingerprint": sourceFingerprint }, body: content });
        }
        catch (cause) {
            updateResearchOperation(prepared.requestId, { status: "unknown" }, prepared, owner ?? undefined);
            keep({ requestId: prepared.requestId, generation: identity.generation, fingerprint: prepared.fingerprint, status: "unknown", filename: file.name, sourceRevision, sourceFingerprint });
            throw cause;
        }
        const payload = await response.json();
        if (!response.ok || !payload.success) {
            updateResearchOperation(prepared.requestId, { status: "unknown" }, prepared, owner ?? undefined);
            keep({ requestId: prepared.requestId, generation: identity.generation, fingerprint: prepared.fingerprint, status: "unknown", filename: file.name, sourceRevision, sourceFingerprint });
            throw new Error(importErrorMessage(payload?.error, "PDF 导入失败。"));
        }
        applyOperation(payload.data, file.name, prepared);
    }
    function applyOperation(
        value: unknown,
        knownFilename?: string,
        expectedOperation?: ResearchPendingOperation,
    ) {
        if (!isCurrent()) return;
        const operation = operationSchema.parse(value) as ResearchOperation;
        const expected = expectedOperation ?? historyState().workspace.pendingOperations?.find(item =>
            (item.kind === "import" || item.kind === "prepare") && item.requestId === record?.requestId && item.generation === record.generation && item.fingerprint === record.fingerprint
        ) ?? null;
        if (!expected || !operationReceiptMatches(operation, expected)) {
            const downgraded = expected
                ? mergeResearchOperationUpdate(expected, { status: "unknown" })
                : null;
            if (expected) updateResearchOperation(expected.requestId, { status: "unknown" }, expected, owner ?? undefined);
            keep({
                ...(downgraded ? sourceRecordFromPending(downgraded) : { status: "unknown" }),
                status: "unknown",
                filename: knownFilename || record?.filename || filename || "PDF",
            });
            setError("导入回执与已保存的请求身份不一致，请检查原请求，不能采用该结果。");
            return;
        }
        const update = operationReceiptUpdate(operation, expected);
        if (!update) {
            const downgrade = { status: "unknown", errorCode: "import_receipt_identity_incomplete" } as const;
            updateResearchOperation(expected.requestId, downgrade, expected, owner ?? undefined);
            keep({ ...sourceRecordFromPending(mergeResearchOperationUpdate(expected, downgrade)), status: "unknown" });
            setError("导入完成回执缺少或改变了已保存的 paper/source identity，未采用该结果。");
            return;
        }
        if (!updateResearchOperation(operation.requestId, update, expected, owner ?? undefined)) {
            setError("导入回执未能写入 owner 工作区，请保留原请求后重试核对。");
            return;
        }
        keep({ ...sourceRecordFromPending(mergeResearchOperationUpdate(expected, update)), filename: knownFilename || record?.filename || filename || "PDF", error: operation.errorCode || operation.message ? { code: operation.errorCode, message: operation.message } : null });
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
                throw new Error(importErrorMessage(payload?.error, "转换状态暂时不可用。"));
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
        if (request.current || !online)
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
        const state = historyState();
        if (!current.current || !state.ready || state.owner !== owner ||
            !record?.paperId || (record.kind !== "prepare" && !record.importId) || !attach.current ||
            !["completed", "passed"].includes(record.status))
            return;
        // Import receipts identify individual operations, not library rows.
        // Only the authenticated backend's canonical paper id can match a PDF.
        const existing = state.workspace.materials?.find(material =>
            (material.kind === "pdf" || material.kind === "paper") && material.referenceId === record.paperId);
        const now = Date.now();
        const incomingReaderHref = isLocalDesktopProfile() ? `/papers/${encodeURIComponent(record.paperId)}/read${record.sourceRevision ? `?sourceRevision=${encodeURIComponent(record.sourceRevision)}` : ""}` : undefined;
        const existingRevision = existing?.readerHref
            ? new URL(existing.readerHref, "https://agora.invalid").searchParams.get("sourceRevision")
            : null;
        // Keep a same-revision locator intact. A genuinely new revision must
        // reach the reader so its existing notebook contract resets old pages.
        const readerHref = existing?.readerHref && (!incomingReaderHref ||
            (record.sourceRevision && existingRevision === record.sourceRevision))
            ? existing.readerHref
            : incomingReaderHref;
        const material = existing
            ? { ...existing, ...(readerHref ? { readerHref } : {}), updatedAt: now }
            : { id: record.importId ?? `source:${record.requestId}`, groupId: null, kind: record.kind === "prepare" ? "paper" as const : "pdf" as const, title: record.filename || filename || "导入的 PDF", url: record.sourceUrl ?? "", referenceId: record.paperId, ...(readerHref ? { readerHref } : {}), notes: "", createdAt: now, updatedAt: now };
        const saved = saveResearchMaterial(material, owner);
        if (saved) {
            setError("");
            attach.current(material.id);
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
        <div
            data-research-source-entry
            data-appearance={appearance}
            data-state={busy ? "processing" : error ? "error" : completed ? "completed" : record ? record.status : "missing"}
            className={`${styles.root} ${record || error || busy || (appearance === "paper-agent" && isLocalDesktopProfile())
                ? "mt-4 flex flex-wrap items-center gap-3 text-sm"
                : "sr-only"}`}
        >
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
            {appearance === "paper-agent" && isLocalDesktopProfile() && <section aria-label="为已识别论文准备来源">
                <label>论文 URL / DOI<input type="text" aria-label="论文来源 URL 或 DOI" value={sourceInput} onChange={event => setSourceInput(event.target.value)} placeholder="https://… 或 10.…/…" /></label>
                <label>目标论文<select aria-label="选择已入库的目标论文" value={selectedTarget ? sourcePaperId : ""} onChange={event => setSourcePaperId(event.target.value)} disabled={busy || targets.length === 0}><option value="">请选择当前论文库中的论文</option>{targets.map(material => <option key={material.id} value={material.referenceId}>{material.title} — {material.referenceId}</option>)}</select></label>
                {selectedTarget && <p>已识别目标：{selectedTarget.title}（真实 ID：<code>{selectedTarget.referenceId}</code>）</p>}
                <p>此入口只为已识别论文补充对应正文；来源解析出的 canonical ID 必须与所选论文一致。不确定或未知的 arXiv / DOI / URL，请先使用上方“在线论文搜索”，选择真实识别结果并保存到库，再在这里选择目标准备来源。搜索成功和元数据入库不代表全文准备成功。</p>
                {targets.length === 0 && <p>当前库中没有可绑定的已识别论文。请先在线检索并保存真实结果，或主动导入 PDF。</p>}
                <p>通过既有 admission 核对身份；只有真实完成回执才可用于研究。解析器未配置、来源不受支持或目标身份不匹配都会显示失败，不会编造身份。</p>
                <button type="button" disabled={busy || !online || !sourceInput.trim() || !selectedTarget} onClick={() => void prepareSource()}>准备所选论文来源</button>
            </section>}
            {busy && (
                <span role="status" data-slot="status" className={`${styles.status} inline-flex items-center gap-2`}>
                    <LoaderCircle size={15} className="animate-spin" />
                    正在核对 {filename || "PDF"} 的真实导入状态…
                </span>
            )}
            {!busy && record && (
                <span role="status" data-slot="status" className={`${styles.status} inline-flex flex-wrap items-center gap-3`}>
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
                                    {attached ? saveConfirmed ? "已加入本次研究" : "入库待保存" : "用于本次研究"}
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
                                    onClick={() => { if (record.kind === "prepare") { setSourceInput(record.sourceUrl ?? ""); setSourcePaperId(record.paperId ?? ""); } else input.current?.click(); }}
                                >
                                    {record.kind === "prepare" ? "修改来源后创建新请求" : "重新选择文件并创建新请求"}
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
            {error && <span role="alert" data-slot="error" className={styles.error}>{error}</span>}
        </div>
    );
}

function identifiedPaper(material: ResearchMaterial): boolean {
    return (material.kind === "paper" || material.kind === "pdf") && typeof material.referenceId === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(material.referenceId);
}

function sourceRecordFromPending(operation: ResearchPendingOperation): ImportRecord {
    if (operation.semantic.kind !== "import" && operation.semantic.kind !== "prepare") return { status: "unknown" };
    const imported = operation.semantic.kind === "import" ? operation.semantic : undefined;
    const prepared = operation.semantic.kind === "prepare" ? operation.semantic : undefined;
    const complete = Boolean(operation.paperId && operation.sourceRevision && operation.sourceFingerprint && (prepared || operation.importId));
    return {
        requestId: operation.requestId, generation: operation.generation, fingerprint: operation.fingerprint,
        kind: imported ? "import" : "prepare", sourceUrl: prepared?.sourceUrl,
        importId: operation.importId, status: operation.status === "prepared" ? "waiting" : operation.status === "passed" && !complete ? "unknown" : operation.status,
        filename: imported?.filename ?? prepared?.sourceUrl, paperId: operation.paperId,
        sourceRevision: operation.sourceRevision ?? imported?.sourceRevision,
        sourceFingerprint: operation.sourceFingerprint ?? imported?.sourceFingerprint,
        updatedAt: operation.updatedAt, error: operation.errorCode ? { code: operation.errorCode } : null,
    };
}

export function normalizeDirectSource(value: string): string | null {
    const input = value.trim().replace(/^doi:\s*/i, "");
    const source = /^10\.\d{4,9}\/\S+$/i.test(input) ? `https://doi.org/${input}` : input;
    const transport = normalizeAnalyzeTransportUrl(source);
    if (!transport) return null;
    const url = new URL(transport);
    if (url.hostname === "localhost" || url.hostname.endsWith(".localhost") || url.hostname === "[::1]" || url.hostname.startsWith("127.") || /^10\./.test(url.hostname) || /^192\.168\./.test(url.hostname) || /^172\.(1[6-9]|2\d|3[01])\./.test(url.hostname)) return null;
    url.hash = "";
    return url.toString();
}

function importStatusMessage(record: ImportRecord): string {
    if (record.status === "failed") {
        return importErrorMessage(record.error, "导入失败，请重新选择文件创建新请求");
    }
    if (record.status === "completed" || record.status === "passed") {
        return "导入回执缺少论文标识，尚不能打开阅读器或用于研究，请检查已有请求";
    }
    if (record.status === "parsing" || record.status === "running") {
        return "PDF 正在处理中，等待真实转换结果";
    }
    if (record.status === "needs_action" || record.status === "rejected") {
        return "结果需要核对";
    }
    if (record.status === "unknown" || record.status === "interrupted") {
        return "导入回执尚未确认，请先检查已有请求";
    }
    return "导入已接收，等待真实处理结果";
}
function importErrorMessage(error: ImportRecord["error"], fallback: string): string {
    // Older saved receipts retained only the service's projected message, not errorCode.
    if (error?.code === "research_parse_unavailable" || error?.message === "Research operation failed (research_parse_unavailable).") {
        return "论文解析运行时尚未配置（research_parse_unavailable），请配置后重新选择文件创建新请求";
    }
    return error?.message || (error?.code ? `${fallback}（${error.code}）` : fallback);
}
async function sha256Hex(content: ArrayBuffer) {
    const digest = await crypto.subtle.digest("SHA-256", content);
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}
