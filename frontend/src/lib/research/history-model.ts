import { isLocalDesktopProfile } from "@/lib/deployment-profile";
import { validResearchConversation, type ResearchConversation } from "./conversation";
import { researchModuleForPath, safeResearchHref, type ResearchModule } from "./entry";
import { normalizeResearchHref } from "./routes";
import { validActivity, validComposerDraft, validMaterial, validPendingOperation, validPrompt, validReaderEvent, validReaderNotebook, validReaderSelection, validReportDraft, validSavedReport, type ResearchActivity, type ResearchComposerDraft, type ResearchMaterial, type ResearchPendingOperation, type ResearchPrompt, type ResearchReaderEvent, type ResearchReaderNotebook, type ResearchReaderSelection, type ResearchReportDraft, type ResearchSavedReport, } from "./workspace-items";
export type ResearchVisit = {
    id: string;
    module: ResearchModule;
    question: string;
    href: string;
    scrollY: number;
    title: string;
    groupId: string | null;
    isFavorite: boolean;
    createdAt: number;
    updatedAt: number;
    deletedAt: number | null;
    archivedAt?: number | null;
    activity?: ResearchActivity | null;
    conversation?: ResearchConversation;
};
export type ResearchGroup = {
    id: string;
    name: string;
    createdAt: number;
    updatedAt: number;
};
export type ResearchWorkspace = {
    visits: ResearchVisit[];
    groups: ResearchGroup[];
    materials?: ResearchMaterial[];
    reportDrafts?: ResearchReportDraft[];
    prompts?: ResearchPrompt[];
    composerDraft?: ResearchComposerDraft | null;
    readerNotebooks?: ResearchReaderNotebook[];
    readerSelections?: ResearchReaderSelection[];
    readerEvents?: ResearchReaderEvent[];
    pendingOperations?: ResearchPendingOperation[];
    savedReports?: ResearchSavedReport[];
};
export type ResearchSnapshot = ResearchWorkspace & {
    revision: number;
};
export const emptyWorkspace = (): ResearchWorkspace => ({ visits: [], groups: [] });
export const sessionParameter = "researchSession";
export const validHistoryId = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9:_-]{1,128}$/.test(value);
const validTime = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value > 0;
export function validateVisit(value: unknown): ResearchVisit | null {
    if (!value || typeof value !== "object")
        return null;
    const visit = value as ResearchVisit;
    const desktop = isLocalDesktopProfile();
    const href = desktop ? normalizeResearchHref(visit.href) : safeResearchHref(visit.href);
    if (!href ||
        !validHistoryId(visit.id) ||
        typeof visit.question !== "string" ||
        !visit.question.trim() ||
        visit.question.length > 2000)
        return null;
    const url = new URL(href, "https://agora.invalid");
    if (researchModuleForPath(url.pathname) !== visit.module)
        return null;
    if (url.searchParams.get("question") !== visit.question)
        return null;
    if (url.searchParams.getAll("question").length !== 1)
        return null;
    if (url.searchParams.getAll(sessionParameter).length > 1)
        return null;
    if (url.searchParams.has(sessionParameter) && url.searchParams.get(sessionParameter) !== visit.id)
        return null;
    if (!validTime(visit.updatedAt) || !validTime(visit.createdAt))
        return null;
    if (!Number.isFinite(visit.scrollY) || visit.scrollY < 0 || visit.scrollY > 1000000)
        return null;
    if (visit.updatedAt < visit.createdAt)
        return null;
    if (typeof visit.title !== "string" || !visit.title.trim() || visit.title.length > 120)
        return null;
    if (typeof visit.isFavorite !== "boolean")
        return null;
    if (visit.groupId !== null && !validHistoryId(visit.groupId))
        return null;
    if (visit.deletedAt !== null && !validTime(visit.deletedAt))
        return null;
    if (visit.archivedAt != null && !validTime(visit.archivedAt))
        return null;
    const activity = visit.activity?.kind === "reader" && desktop
        ? {
            ...visit.activity,
            href: normalizeResearchHref(visit.activity.href) ?? visit.activity.href,
        }
        : visit.activity;
    if (activity != null && !validActivity(activity))
        return null;
    if (activity?.kind === "reader" && visit.module !== "papers")
        return null;
    if (activity?.kind === "report" && visit.module !== "reports")
        return null;
    if (visit.conversation !== undefined &&
        (!validResearchConversation(visit.conversation) ||
            visit.conversation.turns[0].question !== visit.question ||
            !["papers", "projects"].includes(visit.module)))
        return null;
    return {
        id: visit.id,
        module: visit.module,
        question: visit.question,
        href,
        scrollY: visit.scrollY,
        title: visit.title.trim(),
        groupId: visit.groupId,
        isFavorite: visit.isFavorite,
        createdAt: visit.createdAt,
        updatedAt: visit.updatedAt,
        deletedAt: visit.deletedAt,
        ...(visit.archivedAt !== undefined ? { archivedAt: visit.archivedAt } : {}),
        ...(visit.activity !== undefined ? { activity } : {}),
        ...(visit.conversation !== undefined ? { conversation: visit.conversation } : {}),
    };
}
export function validateWorkspace(value: unknown): ResearchWorkspace | null {
    if (!value || typeof value !== "object")
        return null;
    const raw = value as ResearchWorkspace;
    if (!Array.isArray(raw.visits) ||
        !Array.isArray(raw.groups) ||
        raw.visits.length > 2000 ||
        raw.groups.length > 100)
        return null;
    const groups: ResearchGroup[] = [];
    for (const candidate of raw.groups) {
        if (!candidate ||
            !validHistoryId(candidate.id) ||
            typeof candidate.name !== "string" ||
            !candidate.name.trim() ||
            candidate.name.length > 60 ||
            !validTime(candidate.createdAt) ||
            !validTime(candidate.updatedAt))
            return null;
        if (groups.some(group => group.id === candidate.id))
            return null;
        groups.push({ ...candidate, name: candidate.name.trim() });
    }
    const visits: ResearchVisit[] = [];
    for (const candidate of raw.visits) {
        const visit = validateVisit(candidate);
        if (!visit || visits.some(item => item.id === visit.id))
            return null;
        if (visit.groupId && !groups.some(group => group.id === visit.groupId))
            return null;
        visits.push(visit);
    }
    const groupExists = (id: string | null | undefined) => !id || groups.some(group => group.id === id);
    const materials = raw.materials?.map(material => material.readerHref && isLocalDesktopProfile()
        ? {
            ...material,
            readerHref: normalizeResearchHref(material.readerHref) ?? material.readerHref,
        }
        : material);
    if (materials !== undefined && (!Array.isArray(materials) ||
        raw.materials!.length > 1000 ||
        !materials.every(material => validMaterial(material) && groupExists(material.groupId)) ||
        new Set(materials.map(material => material.id)).size !== raw.materials!.length))
        return null;
    if (raw.reportDrafts !== undefined && (!Array.isArray(raw.reportDrafts) ||
        raw.reportDrafts.length > 100 ||
        !raw.reportDrafts.every(draft => validReportDraft(draft) && groupExists(draft.groupId)) ||
        new Set(raw.reportDrafts.map(draft => draft.id)).size !== raw.reportDrafts.length))
        return null;
    if (raw.prompts !== undefined && (!Array.isArray(raw.prompts) ||
        raw.prompts.length > 100 ||
        !raw.prompts.every(validPrompt) ||
        new Set(raw.prompts.map(prompt => prompt.id)).size !== raw.prompts.length))
        return null;
    if (raw.composerDraft != null && (!validComposerDraft(raw.composerDraft) ||
        !groupExists(raw.composerDraft.groupId)))
        return null;
    if (raw.composerDraft?.materialIds?.some(id => !raw.materials?.some(material => material.id === id))) {
        return null;
    }
    if (raw.readerNotebooks !== undefined && (!Array.isArray(raw.readerNotebooks) ||
        raw.readerNotebooks.length > 1000 ||
        !raw.readerNotebooks.every(validReaderNotebook) ||
        new Set(raw.readerNotebooks.map(item => item.paperId)).size !== raw.readerNotebooks.length))
        return null;
    if (raw.readerSelections !== undefined && (!Array.isArray(raw.readerSelections) ||
        raw.readerSelections.length > 5000 ||
        !raw.readerSelections.every(validReaderSelection) ||
        new Set(raw.readerSelections.map(item => item.id)).size !== raw.readerSelections.length))
        return null;
    if (raw.readerEvents !== undefined && (!Array.isArray(raw.readerEvents) ||
        raw.readerEvents.length > 10000 ||
        !raw.readerEvents.every(validReaderEvent) ||
        new Set(raw.readerEvents.map(item => item.id)).size !== raw.readerEvents.length))
        return null;
    const selectionBindings = new Set((raw.readerSelections ?? []).map(item => `${item.paperId}:${item.sourceRevision}:${item.id}`));
    if ((raw.readerEvents ?? []).some(event => event.selectionId &&
        !selectionBindings.has(`${event.paperId}:${event.sourceRevision}:${event.selectionId}`)))
        return null;
    if (raw.pendingOperations !== undefined && (!Array.isArray(raw.pendingOperations) ||
        raw.pendingOperations.length > 500 ||
        !raw.pendingOperations.every(validPendingOperation) ||
        new Set(raw.pendingOperations.map(item => item.requestId)).size !== raw.pendingOperations.length))
        return null;
    if (raw.savedReports !== undefined && (!Array.isArray(raw.savedReports) ||
        raw.savedReports.length > 500 ||
        !raw.savedReports.every(validSavedReport) ||
        new Set(raw.savedReports.map(item => item.id)).size !== raw.savedReports.length))
        return null;
    if (visits.some(visit => {
        const activity = visit.activity;
        return activity?.kind === "report" &&
            !(raw.reportDrafts ?? []).some(draft => draft.id === activity.draftId);
    }))
        return null;
    return {
        visits,
        groups,
        ...(materials !== undefined ? { materials } : {}),
        ...(raw.reportDrafts !== undefined ? { reportDrafts: raw.reportDrafts } : {}),
        ...(raw.prompts !== undefined ? { prompts: raw.prompts } : {}),
        ...(raw.composerDraft !== undefined ? { composerDraft: raw.composerDraft } : {}),
        ...(raw.readerNotebooks !== undefined ? { readerNotebooks: raw.readerNotebooks } : {}),
        ...(raw.readerSelections !== undefined ? { readerSelections: raw.readerSelections } : {}),
        ...(raw.readerEvents !== undefined ? { readerEvents: raw.readerEvents } : {}),
        ...(raw.pendingOperations !== undefined ? { pendingOperations: raw.pendingOperations } : {}),
        ...(raw.savedReports !== undefined ? { savedReports: raw.savedReports } : {}),
    };
}
// Deterministic identity deduplicates the same v1 record across imports.
export function legacyHistoryId(value: string): string {
    let first = 2166136261;
    let second = 5381;
    for (let index = 0; index < value.length; index += 1) {
        first = Math.imul(first ^ value.charCodeAt(index), 16777619);
        second = Math.imul(second, 33) ^ value.charCodeAt(index);
    }
    return `legacy-${(first >>> 0).toString(16)}-${(second >>> 0).toString(16)}`;
}
export function migrateLegacyHistory(raw: unknown): ResearchWorkspace {
    if (!Array.isArray(raw))
        return emptyWorkspace();
    const visits = raw.slice(0, 100).flatMap(candidate => {
        if (!candidate ||
            typeof candidate.question !== "string" ||
            candidate.id !== `${candidate.module}:${candidate.question}`)
            return [];
        const visit = validateVisit({
            ...candidate,
            id: legacyHistoryId(candidate.id),
            title: candidate.question.slice(0, 40),
            groupId: null,
            isFavorite: false,
            createdAt: candidate.updatedAt,
            deletedAt: null,
        });
        return visit ? [visit] : [];
    });
    return {
        visits: visits.filter((visit, index) => visits.findIndex(item => item.id === visit.id) === index),
        groups: [],
    };
}
