import { isResearchMode, type ResearchMode } from "./entry";
export type ResearchConstraints = {
    recentYear?: boolean;
    hasCode?: boolean;
    paperType?: "survey";
    language?: "python" | "typescript" | "javascript" | "rust" | "go";
    license?: "MIT" | "Apache-2.0" | "BSD-3-Clause";
    recentlyActive?: boolean;
    localRunnable?: boolean;
};
export type ResearchMaterial = {
    id: string;
    groupId: string | null;
    kind: "paper" | "project" | "discussion" | "note" | "pdf";
    title: string;
    url: string;
    referenceId?: string;
    readerHref?: string;
    notes: string;
    favorite?: boolean;
    readingStatus?: "unread" | "reading" | "read";
    tags?: string[];
    createdAt: number;
    updatedAt: number;
};
export type ResearchReaderNotebook = {
    paperId: string;
    sourceRevision: string;
    note: string;
    question: string;
    fontSize: 16 | 18 | 20 | 22;
    sectionId: string;
    pdfPage: number;
    readerView?: "text" | "pdf";
    updatedAt: number;
};
export type ResearchReaderSelection = {
    id: string;
    paperId: string;
    sourceRevision: string;
    sectionId: string;
    paragraphId: string;
    blockId?: string;
    pageNumber?: number;
    selectedText: string;
    surroundingText: string;
    startOffset: number;
    endOffset: number;
    noteText: string;
    explainQuestion: string;
    explainAnswer?: string;
    exampleQuestion: string;
    exampleAnswer?: string;
    explained: boolean;
    exampled: boolean;
    confused: boolean;
    createdAt: number;
    updatedAt: number;
};
export type ResearchReaderEvent = {
    id: string;
    type:
        | "selection_created"
        | "selection_discarded"
        | "note_updated"
        | "explanation_generated"
        | "example_generated"
        | "confusion_marked"
        | "confusion_unmarked"
        | "reader_settings_changed"
        | "drawer_resized"
        | "toc_navigated";
    paperId: string;
    sourceRevision: string;
    selectionId?: string;
    sectionId?: string;
    paragraphId?: string;
    pageNumber?: number;
    selectedText?: string;
    surroundingText?: string;
    createdAt: number;
};
export type ResearchReaderSelectionState = {
    paperId: string;
    sourceRevision: string;
    selections: ResearchReaderSelection[];
    events: ResearchReaderEvent[];
    updatedAt: number;
};
export type ResearchOperationKind = "prepare" | "import" | "analyze" | "ask";
export type ResearchOperationStatus = "prepared" | "waiting" | "running" | "needs_action" | "passed" | "rejected" | "failed" | "interrupted" | "unknown";
export type ResearchPrepareSemantic = {
    kind: "prepare";
    paperId: string;
    sourceUrl: string;
    sourceType: string | null;
};
export type ResearchImportSemantic = {
    kind: "import";
    filename: string;
    sizeBytes: number;
    sourceRevision: string;
    sourceFingerprint: string;
};
export type ResearchPersonalMemoryRef = {
    memoryId: string;
    revision: number;
};
export type ResearchPersonalMemorySelection = {
    refs: ResearchPersonalMemoryRef[];
    policyRevision: number;
};
export type ResearchAnalyzeSemantic = {
    kind: "analyze";
    paperId: string;
    sourceRevision: string;
    sourceFingerprint: string;
    sourceUrl: string | null;
    pdfUrl: string | null;
    objective: string | null;
    constraints: string[];
    personalMemory?: ResearchPersonalMemorySelection;
};
export type ResearchAskSelection = {
    sectionId?: string;
    sectionIndex?: number;
    page?: number;
    pdfPage?: number;
    selectedText?: string;
};
export type ResearchAskSemantic = {
    kind: "ask";
    runId: string;
    paperId: string;
    sourceRevision: string;
    sourceFingerprint: string;
    question: string;
    selection: ResearchAskSelection;
    locale: string | null;
    personalMemory?: ResearchPersonalMemorySelection;
};
export type ResearchOperationSemantic = ResearchPrepareSemantic | ResearchImportSemantic | ResearchAnalyzeSemantic | ResearchAskSemantic;
export type ResearchPendingOperation = {
    requestId: string;
    generation: string;
    kind: ResearchOperationKind;
    fingerprint: string;
    semantic: ResearchOperationSemantic;
    status: ResearchOperationStatus;
    paperId?: string;
    sourceRevision?: string;
    sourceFingerprint?: string;
    importId?: string;
    runId?: string;
    question?: string;
    errorCode?: string;
    createdAt: number;
    updatedAt: number;
};
export type ResearchSavedReport = {
    id: string;
    reportId?: string;
    runId?: string;
    draftId?: string;
    kind: "accepted" | "draft";
    title: string;
    savedAt: number;
};
export type ResearchReportMaterial = {
    id: string;
    title: string;
    url: string;
    notes: string;
};
export type ResearchReportDraft = {
    id: string;
    groupId?: string | null;
    question: string;
    title: string;
    scope: string;
    notes: string;
    materials: ResearchReportMaterial[];
    updatedAt: number;
};
export type ResearchPrompt = {
    id: string;
    name: string;
    question: string;
    mode: ResearchMode;
    constraints: ResearchConstraints;
    updatedAt: number;
};
export type ResearchComposerDraft = {
    question: string;
    mode: ResearchMode;
    constraints: ResearchConstraints;
    groupId: string | null;
    updatedAt: number;
    materialIds?: string[];
    submittedSessionId?: string;
};
export type ResearchActivity = {
    kind: "reader";
    paperId: string;
    title: string;
    href: string;
    sourceRevision?: string;
    sectionId?: string;
    sectionTitle?: string;
    pdfPage?: number;
    updatedAt: number;
} | {
    kind: "report";
    draftId: string;
    title: string;
    updatedAt: number;
};
export const validWorkspaceId = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9:_-]{1,128}$/.test(value);
export const validWorkspaceTime = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value > 0;
const text = (value: unknown, maximum: number, required = false): value is string => typeof value === "string" &&
    value.length <= maximum &&
    (!required || Boolean(value.trim())) &&
    !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value);
const group = (value: unknown): value is string | null => value === null || validWorkspaceId(value);
const sha = (value: unknown): value is string => typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value);
export function safeWorkspaceSource(value: unknown): value is string {
    if (!text(value, 2000) || /[\r\n\\]/.test(value))
        return false;
    if (!value)
        return true;
    try {
        const url = new URL(value);
        return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password;
    }
    catch {
        return false;
    }
}
export function safeWorkspaceReader(value: unknown): value is string {
    if (
        !text(value, 10000, true) ||
        !value.startsWith("/") ||
        value.startsWith("//") ||
        /[\\\r\n]/.test(value)
    ) {
        return false;
    }

    try {
        const url = new URL(value, "https://agora.invalid");
        const match = url.pathname.match(
            /^\/(?:design-demo\/)?papers\/([^/]+)\/read$/,
        );
        if (url.origin !== "https://agora.invalid" || url.hash || !match) {
            return false;
        }

        let paperId: string;
        try {
            paperId = decodeURIComponent(match[1]);
        } catch {
            return false;
        }
        const parts = paperId.split("/");
        if (
            !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$/.test(paperId) ||
            parts.some((part) => !part || part === "." || part === "..")
        ) {
            return false;
        }

        const allowedKeys = new Set([
            "sourceRevision",
            "resumeSection",
            "resumePage",
            "returnTo",
        ]);
        const keys = [...url.searchParams.keys()];
        if (
            keys.some(
                (key) =>
                    !allowedKeys.has(key) ||
                    url.searchParams.getAll(key).length !== 1,
            )
        ) {
            return false;
        }

        const sourceRevision = url.searchParams.get("sourceRevision");
        if (sourceRevision !== null && !text(sourceRevision, 512, true)) {
            return false;
        }
        const resumeSection = url.searchParams.get("resumeSection");
        if (resumeSection !== null && !text(resumeSection, 200, true)) {
            return false;
        }
        const resumePage = url.searchParams.get("resumePage");
        if (
            resumePage !== null &&
            (!/^[1-9]\d{0,4}$/.test(resumePage) || Number(resumePage) > 10000)
        ) {
            return false;
        }

        const returnTo = url.searchParams.get("returnTo");
        if (returnTo !== null) {
            if (
                !text(returnTo, 10000, true) ||
                !returnTo.startsWith("/") ||
                returnTo.startsWith("//") ||
                /[\\\r\n]/.test(returnTo)
            ) {
                return false;
            }
            const returnUrl = new URL(returnTo, "https://agora.invalid");
            if (
                returnUrl.origin !== "https://agora.invalid" ||
                returnUrl.username ||
                returnUrl.password
            ) {
                return false;
            }
        }
        return true;
    } catch {
        return false;
    }
}
export function validConstraints(value: unknown): value is ResearchConstraints {
    if (!value || typeof value !== "object" || Array.isArray(value))
        return false;
    const constraints = value as ResearchConstraints;
    const allowed = [
        "recentYear", "hasCode", "paperType", "language", "license",
        "recentlyActive", "localRunnable",
    ];
    return (Object.keys(constraints).every(key => allowed.includes(key)) &&
        [constraints.recentYear, constraints.hasCode, constraints.recentlyActive, constraints.localRunnable]
            .every(item => item === undefined || typeof item === "boolean") &&
        (constraints.paperType === undefined || constraints.paperType === "survey") &&
        (constraints.language === undefined || ["python", "typescript", "javascript", "rust", "go"].includes(constraints.language)) &&
        (constraints.license === undefined || ["MIT", "Apache-2.0", "BSD-3-Clause"].includes(constraints.license)));
}
function validMaterialTags(value: unknown): value is string[] {
    if (!Array.isArray(value) || value.length > 32) return false;
    const normalized = new Set<string>();
    for (const tag of value) {
        if (typeof tag !== "string" || tag.length < 1 || tag.length > 64 || tag !== tag.trim()) return false;
        const key = tag.toLowerCase();
        if (normalized.has(key)) return false;
        normalized.add(key);
    }
    return true;
}

export function validMaterial(value: unknown): value is ResearchMaterial {
    if (!value || typeof value !== "object")
        return false;
    const material = value as ResearchMaterial & {
        favorite?: unknown;
        readingStatus?: unknown;
        tags?: unknown;
    };
    const favorite = material.favorite === undefined ? false : material.favorite;
    const readingStatus = material.readingStatus === undefined ? "unread" : material.readingStatus;
    const tags = material.tags === undefined ? [] : material.tags;
    const valid = (validWorkspaceId(material.id) &&
        group(material.groupId) &&
        ["paper", "project", "discussion", "note", "pdf"].includes(material.kind) &&
        text(material.title, 500, true) &&
        safeWorkspaceSource(material.url) &&
        (material.kind === "note" || Boolean(material.url) || Boolean(material.readerHref) ||
            (material.kind === "pdf" && Boolean(material.referenceId))) &&
        text(material.notes, 10000) &&
        typeof favorite === "boolean" &&
        ["unread", "reading", "read"].includes(String(readingStatus)) &&
        validMaterialTags(tags) &&
        (material.referenceId === undefined || text(material.referenceId, 256, true)) &&
        (material.readerHref === undefined || safeWorkspaceReader(material.readerHref)) &&
        validWorkspaceTime(material.createdAt) &&
        validWorkspaceTime(material.updatedAt) &&
        material.updatedAt >= material.createdAt);
    if (!valid) return false;
    if (material.favorite === undefined) material.favorite = false;
    if (material.readingStatus === undefined) material.readingStatus = "unread";
    if (material.tags === undefined) material.tags = [];
    return true;
}
export function validReportDraft(value: unknown): value is ResearchReportDraft {
    if (!value || typeof value !== "object")
        return false;
    const draft = value as ResearchReportDraft;
    return (validWorkspaceId(draft.id) &&
        (draft.groupId === undefined || group(draft.groupId)) &&
        text(draft.question, 2000) &&
        text(draft.title, 2000, true) &&
        text(draft.scope, 10000) &&
        text(draft.notes, 30000) &&
        validWorkspaceTime(draft.updatedAt) &&
        Array.isArray(draft.materials) &&
        draft.materials.length <= 100 &&
        draft.materials.every(material => Boolean(material) &&
            validWorkspaceId(material.id) &&
            text(material.title, 500) &&
            safeWorkspaceSource(material.url) &&
            text(material.notes, 10000)) &&
        new Set(draft.materials.map(material => material.id)).size === draft.materials.length);
}
export function validPrompt(value: unknown): value is ResearchPrompt {
    if (!value || typeof value !== "object")
        return false;
    const prompt = value as ResearchPrompt;
    return (validWorkspaceId(prompt.id) &&
        text(prompt.name, 80, true) &&
        text(prompt.question, 2000, true) &&
        isResearchMode(prompt.mode) &&
        validConstraints(prompt.constraints) &&
        validWorkspaceTime(prompt.updatedAt));
}
export function validComposerDraft(value: unknown): value is ResearchComposerDraft {
    if (!value || typeof value !== "object")
        return false;
    const draft = value as ResearchComposerDraft;
    return (text(draft.question, 2000) &&
        isResearchMode(draft.mode) &&
        validConstraints(draft.constraints) &&
        group(draft.groupId) &&
        validWorkspaceTime(draft.updatedAt) &&
        (draft.materialIds === undefined || (Array.isArray(draft.materialIds) &&
            draft.materialIds.length <= 50 &&
            draft.materialIds.every(validWorkspaceId) &&
            new Set(draft.materialIds).size === draft.materialIds.length)) &&
        (draft.submittedSessionId === undefined || validWorkspaceId(draft.submittedSessionId)));
}
export function validActivity(value: unknown): value is ResearchActivity {
    if (!value || typeof value !== "object")
        return false;
    const activity = value as ResearchActivity;
    if (!text(activity.title, 500, true) || !validWorkspaceTime(activity.updatedAt))
        return false;
    if (activity.kind === "report")
        return validWorkspaceId(activity.draftId);
    if (activity.kind !== "reader")
        return false;
    const hasPreciseLocator =
        activity.sectionId !== undefined || activity.pdfPage !== undefined;
    return (
        text(activity.paperId, 256, true) &&
        safeWorkspaceReader(activity.href) &&
        (activity.sourceRevision === undefined ||
            text(activity.sourceRevision, 512, true)) &&
        (!hasPreciseLocator || activity.sourceRevision !== undefined) &&
        (activity.sectionId === undefined || text(activity.sectionId, 200, true)) &&
        (activity.sectionTitle === undefined ||
            text(activity.sectionTitle, 500)) &&
        (activity.pdfPage === undefined ||
            (Number.isInteger(activity.pdfPage) &&
                activity.pdfPage > 0 &&
                activity.pdfPage <= 10000))
    );
}
export function validReaderSelection(value: unknown): value is ResearchReaderSelection {
    if (!value || typeof value !== "object")
        return false;
    const item = value as ResearchReaderSelection;
    return (text(item.id, 128, true) &&
        text(item.paperId, 200, true) &&
        text(item.sourceRevision, 512, true) &&
        text(item.sectionId, 200, true) &&
        text(item.paragraphId, 200, true) &&
        (item.blockId === undefined || text(item.blockId, 200)) &&
        (item.pageNumber === undefined || (Number.isSafeInteger(item.pageNumber) && item.pageNumber > 0 && item.pageNumber <= 10000)) &&
        text(item.selectedText, 10000, true) &&
        text(item.surroundingText, 20000) &&
        Number.isSafeInteger(item.startOffset) && item.startOffset >= 0 &&
        Number.isSafeInteger(item.endOffset) && item.endOffset > item.startOffset &&
        item.endOffset - item.startOffset <= item.selectedText.length + 1000 &&
        text(item.noteText, 10000) &&
        text(item.explainQuestion, 2000) &&
        (item.explainAnswer === undefined || text(item.explainAnswer, 30000)) &&
        text(item.exampleQuestion, 2000) &&
        (item.exampleAnswer === undefined || text(item.exampleAnswer, 30000)) &&
        typeof item.explained === "boolean" &&
        typeof item.exampled === "boolean" &&
        typeof item.confused === "boolean" &&
        validWorkspaceTime(item.createdAt) &&
        validWorkspaceTime(item.updatedAt) &&
        item.updatedAt >= item.createdAt);
}
const readerEventTypes: ResearchReaderEvent["type"][] = [
    "selection_created", "selection_discarded", "note_updated", "explanation_generated",
    "example_generated", "confusion_marked", "confusion_unmarked",
    "reader_settings_changed", "drawer_resized", "toc_navigated",
];
export function validReaderEvent(value: unknown): value is ResearchReaderEvent {
    if (!value || typeof value !== "object")
        return false;
    const item = value as ResearchReaderEvent;
    if (!text(item.id, 128, true) ||
        !readerEventTypes.includes(item.type) ||
        !text(item.paperId, 200, true) ||
        !text(item.sourceRevision, 512, true) ||
        !validWorkspaceTime(item.createdAt))
        return false;
    if (item.selectionId != null && !text(item.selectionId, 128, true))
        return false;
    if (item.paragraphId != null && !text(item.paragraphId, 200))
        return false;
    if (item.sectionId != null && !text(item.sectionId, 200))
        return false;
    if (item.pageNumber != null && (!Number.isSafeInteger(item.pageNumber) || item.pageNumber <= 0 || item.pageNumber > 10000))
        return false;
    if (item.selectedText != null && !text(item.selectedText, 10000))
        return false;
    if (item.surroundingText != null && !text(item.surroundingText, 20000))
        return false;
    return true;
}
export function validReaderNotebook(value: unknown): value is ResearchReaderNotebook {
    if (!value || typeof value !== "object" || Array.isArray(value))
        return false;
    const item = value as ResearchReaderNotebook;
    const allowed = ["paperId", "sourceRevision", "note", "question", "fontSize", "sectionId", "pdfPage", "readerView", "updatedAt"];
    return (Object.keys(item).every(key => allowed.includes(key)) &&
        text(item.paperId, 200, true) &&
        text(item.sourceRevision, 512, true) &&
        text(item.note, 20000) &&
        text(item.question, 2000) &&
        [16, 18, 20, 22].includes(item.fontSize) &&
        text(item.sectionId, 200) &&
        Number.isInteger(item.pdfPage) && item.pdfPage > 0 && item.pdfPage <= 10000 &&
        (item.readerView === undefined || item.readerView === "text" || item.readerView === "pdf") &&
        validWorkspaceTime(item.updatedAt));
}
function validAskSelection(value: unknown): value is ResearchAskSelection {
    if (!value || typeof value !== "object" || Array.isArray(value))
        return false;
    const selection = value as Record<string, unknown>;
    const allowed = ["sectionId", "sectionIndex", "page", "pdfPage", "selectedText"];
    if (Object.keys(selection).some(key => !allowed.includes(key)))
        return false;
    if (selection.sectionId !== undefined && !text(selection.sectionId, 200, true))
        return false;
    if (selection.selectedText !== undefined && !text(selection.selectedText, 10000, true))
        return false;
    for (const field of ["sectionIndex", "page", "pdfPage"] as const) {
        const number = selection[field];
        if (number !== undefined && (typeof number !== "number" || !Number.isSafeInteger(number) || number < 0 || number > 10000))
            return false;
    }
    try {
        return new TextEncoder().encode(JSON.stringify(selection)).byteLength <= 32000;
    }
    catch {
        return false;
    }
}
function validPersonalMemorySelection(value: unknown): value is ResearchPersonalMemorySelection {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const selection = value as Record<string, unknown>;
    if (Object.keys(selection).length !== 2 || !Object.keys(selection).every(key => ["refs", "policyRevision"].includes(key))) return false;
    if (!Number.isSafeInteger(selection.policyRevision) || Number(selection.policyRevision) < 1) return false;
    if (!Array.isArray(selection.refs) || selection.refs.length < 1 || selection.refs.length > 20) return false;
    const ids = new Set<string>();
    for (const raw of selection.refs) {
        if (!raw || typeof raw !== "object" || Array.isArray(raw)) return false;
        const ref = raw as Record<string, unknown>;
        if (Object.keys(ref).length !== 2 || !Object.keys(ref).every(key => ["memoryId", "revision"].includes(key))) return false;
        if (typeof ref.memoryId !== "string" || !/^pmem_[0-9a-f]{32}$/.test(ref.memoryId)) return false;
        if (!Number.isSafeInteger(ref.revision) || Number(ref.revision) < 1 || ids.has(ref.memoryId)) return false;
        ids.add(ref.memoryId);
    }
    return true;
}

export function validOperationSemantic(value: unknown, kind?: ResearchOperationKind): value is ResearchOperationSemantic {
    if (!value || typeof value !== "object" || Array.isArray(value))
        return false;
    const semantic = value as Record<string, unknown>;
    if (kind && semantic.kind !== kind)
        return false;
    if (semantic.kind === "prepare") {
        const allowed = ["kind", "paperId", "sourceUrl", "sourceType"];
        return (Object.keys(semantic).every(key => allowed.includes(key)) &&
            text(semantic.paperId, 256, true) &&
            safeWorkspaceSource(semantic.sourceUrl) &&
            Boolean(semantic.sourceUrl) &&
            (semantic.sourceType === null || text(semantic.sourceType, 64, true)));
    }
    if (semantic.kind === "import") {
        const allowed = ["kind", "filename", "sizeBytes", "sourceRevision", "sourceFingerprint"];
        return (Object.keys(semantic).every(key => allowed.includes(key)) &&
            text(semantic.filename, 255, true) &&
            Number.isSafeInteger(semantic.sizeBytes) &&
            Number(semantic.sizeBytes) > 0 &&
            Number(semantic.sizeBytes) <= 50 * 1024 * 1024 &&
            text(semantic.sourceRevision, 512, true) &&
            sha(semantic.sourceFingerprint));
    }
    if (semantic.kind === "analyze") {
        const allowed = [
            "kind", "paperId", "sourceRevision", "sourceFingerprint", "sourceUrl",
            "pdfUrl", "objective", "constraints", "personalMemory",
        ];
        return (Object.keys(semantic).every(key => allowed.includes(key)) &&
            text(semantic.paperId, 256, true) &&
            text(semantic.sourceRevision, 512, true) &&
            sha(semantic.sourceFingerprint) &&
            (semantic.sourceUrl === null || safeWorkspaceSource(semantic.sourceUrl)) &&
            (semantic.pdfUrl === null || safeWorkspaceSource(semantic.pdfUrl)) &&
            (semantic.objective === null || text(semantic.objective, 2000, true)) &&
            Array.isArray(semantic.constraints) &&
            semantic.constraints.length <= 20 &&
            semantic.constraints.every(item => text(item, 500, true)) &&
            (semantic.personalMemory === undefined || validPersonalMemorySelection(semantic.personalMemory)));
    }
    if (semantic.kind === "ask") {
        const allowed = [
            "kind", "runId", "paperId", "sourceRevision", "sourceFingerprint",
            "question", "selection", "locale", "personalMemory",
        ];
        return (Object.keys(semantic).every(key => allowed.includes(key)) &&
            text(semantic.runId, 256, true) &&
            text(semantic.paperId, 256, true) &&
            text(semantic.sourceRevision, 512, true) &&
            sha(semantic.sourceFingerprint) &&
            text(semantic.question, 2000, true) &&
            validAskSelection(semantic.selection) &&
            (semantic.locale === null || text(semantic.locale, 32, true)) &&
            (semantic.personalMemory === undefined || validPersonalMemorySelection(semantic.personalMemory)));
    }
    return false;
}
export function validPendingOperation(value: unknown): value is ResearchPendingOperation {
    if (!value || typeof value !== "object")
        return false;
    const item = value as ResearchPendingOperation;
    return (validWorkspaceId(item.requestId) &&
        /^gen_[0-9a-f]{32}$/.test(item.generation) &&
        ["prepare", "import", "analyze", "ask"].includes(item.kind) &&
        sha(item.fingerprint) &&
        validOperationSemantic(item.semantic, item.kind) &&
        pendingSemanticIdentityMatches(item) &&
        ["prepared", "waiting", "running", "needs_action", "passed", "rejected", "failed", "interrupted", "unknown"].includes(item.status) &&
        (item.paperId === undefined || text(item.paperId, 200, true)) &&
        (item.sourceRevision === undefined || text(item.sourceRevision, 512, true)) &&
        (item.sourceFingerprint === undefined || sha(item.sourceFingerprint)) &&
        (item.importId === undefined || text(item.importId, 256, true)) &&
        (item.runId === undefined || text(item.runId, 256, true)) &&
        (item.question === undefined || text(item.question, 4000)) &&
        (item.errorCode === undefined || text(item.errorCode, 200, true)) &&
        validWorkspaceTime(item.createdAt) &&
        validWorkspaceTime(item.updatedAt) &&
        item.updatedAt >= item.createdAt);
}
function pendingSemanticIdentityMatches(item: ResearchPendingOperation): boolean {
    const semantic = item.semantic;
    // Import paper identity and prepare source identity exist only on a complete,
    // passed receipt. Input-fixed semantic fields remain immutable on recovery.
    if (semantic.kind === "import") {
        if (item.sourceRevision !== undefined && item.sourceRevision !== semantic.sourceRevision)
            return false;
        if (item.sourceFingerprint !== undefined && item.sourceFingerprint !== semantic.sourceFingerprint)
            return false;
        if (item.paperId !== undefined && (item.status !== "passed" || item.importId === undefined || item.sourceRevision === undefined || item.sourceFingerprint === undefined))
            return false;
    }
    else if (semantic.kind === "prepare") {
        if (item.paperId !== undefined && item.paperId !== semantic.paperId)
            return false;
        const hasSourceOutput = item.sourceRevision !== undefined || item.sourceFingerprint !== undefined;
        if (hasSourceOutput && (item.status !== "passed" || item.sourceRevision === undefined || item.sourceFingerprint === undefined))
            return false;
    }
    else {
        if (item.paperId !== undefined && item.paperId !== semantic.paperId)
            return false;
        if (item.sourceRevision !== undefined && item.sourceRevision !== semantic.sourceRevision)
            return false;
        if (item.sourceFingerprint !== undefined && item.sourceFingerprint !== semantic.sourceFingerprint)
            return false;
    }
    return item.question === undefined || (semantic.kind === "ask" && item.question === semantic.question);
}
export function validSavedReport(value: unknown): value is ResearchSavedReport {
    if (!value || typeof value !== "object")
        return false;
    const item = value as ResearchSavedReport;
    return (validWorkspaceId(item.id) &&
        ["accepted", "draft"].includes(item.kind) &&
        text(item.title, 500, true) &&
        validWorkspaceTime(item.savedAt) &&
        (item.reportId === undefined || text(item.reportId, 200, true)) &&
        (item.runId === undefined || text(item.runId, 256, true)) &&
        (item.draftId === undefined || validWorkspaceId(item.draftId)) &&
        (item.kind === "accepted" ? Boolean(item.reportId && item.runId) : Boolean(item.draftId)));
}
