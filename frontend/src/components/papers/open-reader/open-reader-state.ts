"use client";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { ReaderEvent as BackendReaderEvent, ReaderMaterialSummary as BackendReaderMaterialSummary, ReaderSelection as BackendReaderSelection } from "@/lib/papers/types";
import type { ReaderEvent, ReaderEventType, ReaderSelection, ReaderSettings, ReaderTheme, ReaderParagraph } from "./open-reader-types";
import { isLocalDesktopProfile } from "@/lib/deployment-profile";
import { saveReaderSelectionState } from "@/lib/research/history";
import { useOwnedResearchWorkspace } from "@/lib/research/use-research-history";
import type { ResearchReaderEvent, ResearchReaderSelection } from "@/lib/research/workspace-items";
import { clamp, createId, createReaderEvent, DEFAULT_READER_SETTINGS, getSurroundingText, nowIso, READER_SETTINGS_LAYOUT_VERSION, safeJsonParse, shouldKeepSelection, storageKey } from "./open-reader-utils";
interface SelectionState {
    selections: ReaderSelection[];
    events: ReaderEvent[];
}
type SelectionAction = {
    type: "load";
    state: SelectionState;
} | {
    type: "create_temp";
    selection: ReaderSelection;
} | {
    type: "discard";
    selectionId: string;
} | {
    type: "discard_all_temp";
} | {
    type: "update_note";
    selectionId: string;
    noteText: string;
} | {
    type: "confirm_explain";
    selectionId: string;
    question: string;
    answer: string;
} | {
    type: "confirm_example";
    selectionId: string;
    question: string;
    answer: string;
} | {
    type: "toggle_confused";
    selectionId: string;
} | {
    type: "merge_remote";
    state: SelectionState;
};
const EMPTY_STATE: SelectionState = { selections: [], events: [] };
export function useOpenReaderSettings(paperId: string) {
    const key = useMemo(() => storageKey(paperId, "settings"), [paperId]);
    const [settingsLoaded, setSettingsLoaded] = useState(false);
    const [storageError, setStorageError] = useState(false);
    const [settings, setSettings] = useReducer((current: ReaderSettings, patch: Partial<ReaderSettings>) => {
        const next: ReaderSettings = { ...current, ...patch, layoutVersion: READER_SETTINGS_LAYOUT_VERSION };
        next.fontSize = clamp(next.fontSize, 12, 38);
        next.contentWidth = clamp(next.contentWidth, 520, 2000);
        next.drawerWidth = clamp(next.drawerWidth, 360, 920);
        next.theme = normalizeTheme(next.theme);
        return next;
    }, DEFAULT_READER_SETTINGS);
    useEffect(() => {
        if (typeof window === "undefined")
            return;
        try {
            setSettings(normalizeStoredSettings(safeJsonParse<Partial<ReaderSettings>>(window.localStorage.getItem(key), {})));
        }
        catch {
            setStorageError(true);
        }
        setSettingsLoaded(true);
    }, [key]);
    useEffect(() => {
        if (typeof window === "undefined" || !settingsLoaded)
            return;
        try {
            window.localStorage.setItem(key, JSON.stringify(settings));
            setStorageError(false);
        }
        catch {
            setStorageError(true);
        }
    }, [key, settings, settingsLoaded]);
    return { settings, patchSettings: setSettings, storageError };
}
function normalizeStoredSettings(stored: Partial<ReaderSettings>): ReaderSettings {
    const shouldUpgradeWidth = stored.layoutVersion !== READER_SETTINGS_LAYOUT_VERSION &&
        (stored.contentWidth ?? DEFAULT_READER_SETTINGS.contentWidth) < DEFAULT_READER_SETTINGS.contentWidth;
    return {
        ...DEFAULT_READER_SETTINGS,
        ...stored,
        contentWidth: shouldUpgradeWidth ? DEFAULT_READER_SETTINGS.contentWidth : stored.contentWidth ?? DEFAULT_READER_SETTINGS.contentWidth,
        layoutVersion: READER_SETTINGS_LAYOUT_VERSION,
    };
}
function normalizeTheme(value: string): ReaderTheme {
    if (value === "light" || value === "warm" || value === "dark")
        return value;
    return "warm";
}
export function useOpenReaderSelections(paperId: string, sourceRevision?: string) {
    const selectionsKey = useMemo(() => storageKey(paperId, "selections"), [paperId]);
    const eventsKey = useMemo(() => storageKey(paperId, "events"), [paperId]);
    const workspace = useOwnedResearchWorkspace();
    const ownedDesktop = isLocalDesktopProfile() && Boolean(sourceRevision);
    const [state, dispatch] = useReducer(selectionReducer, EMPTY_STATE);
    const [selectionsLoaded, setSelectionsLoaded] = useState(false);
    const [storageError, setStorageError] = useState(false);
    const [legacyAvailable, setLegacyAvailable] = useState(false);
    const skipNextSave = useRef(false);
    const loadedScope = useRef("");
    useEffect(() => {
        if (typeof window === "undefined" || (ownedDesktop && !workspace.ready))
            return;
        setSelectionsLoaded(false);
        loadedScope.current = "";
        skipNextSave.current = true;
        dispatch({ type: "load", state: EMPTY_STATE });
        try {
            if (ownedDesktop) {
                const storedSelections = (workspace.workspace.readerSelections ?? [])
                    .filter(item => item.paperId === paperId && item.sourceRevision === sourceRevision)
                    .map(fromResearchSelection);
                const storedEvents = (workspace.workspace.readerEvents ?? [])
                    .filter(item => item.paperId === paperId && item.sourceRevision === sourceRevision)
                    .map(fromResearchEvent);
                dispatch({
                    type: "load",
                    state: { selections: storedSelections, events: storedEvents },
                });
                const legacySelections = safeJsonParse<unknown[]>(window.localStorage.getItem(selectionsKey), []);
                const legacyEvents = safeJsonParse<unknown[]>(window.localStorage.getItem(eventsKey), []);
                const legacy = normalizeLoadedSelectionState(paperId, legacySelections, legacyEvents);
                setLegacyAvailable(legacy.selections.length > 0 || legacy.events.length > 0);
            }
            else {
                const storedSelections = safeJsonParse<unknown[]>(window.localStorage.getItem(selectionsKey), []);
                const storedEvents = safeJsonParse<unknown[]>(window.localStorage.getItem(eventsKey), []);
                dispatch({
                    type: "load",
                    state: normalizeLoadedSelectionState(paperId, storedSelections, storedEvents),
                });
            }
            setStorageError(false);
        }
        catch {
            dispatch({ type: "load", state: EMPTY_STATE });
            setStorageError(true);
        }
        loadedScope.current = selectionScope(ownedDesktop, workspace.owner, paperId, sourceRevision);
        setSelectionsLoaded(true);
    }, [eventsKey, ownedDesktop, paperId, selectionsKey, sourceRevision, workspace.owner, workspace.ready, workspace.workspace.readerEvents, workspace.workspace.readerSelections]);
    useEffect(() => {
        if (typeof window === "undefined" ||
            !selectionsLoaded ||
            loadedScope.current !== selectionScope(ownedDesktop, workspace.owner, paperId, sourceRevision))
            return;
        if (skipNextSave.current) {
            skipNextSave.current = false;
            return;
        }
        if (ownedDesktop) {
            if (!workspace.ready)
                return;
            if (!workspace.owner || !sourceRevision) {
                setStorageError(true);
                return;
            }
            const persistedSelections = state.selections
                .filter(shouldKeepSelection)
                .slice(-500)
                .map(selection => toResearchSelection(selection, sourceRevision));
            const selectionIds = new Set(persistedSelections.map(selection => selection.id));
            const persistedEvents = state.events
                .filter(event => !event.selectionId || selectionIds.has(event.selectionId))
                .slice(-2000)
                .map(event => toResearchEvent(event, sourceRevision));
            setStorageError(!saveReaderSelectionState({
                paperId,
                sourceRevision,
                selections: persistedSelections,
                events: persistedEvents,
                updatedAt: Date.now(),
            }, workspace.owner));
            return;
        }
        try {
            window.localStorage.setItem(selectionsKey, JSON.stringify(state.selections));
            window.localStorage.setItem(eventsKey, JSON.stringify(state.events));
            setStorageError(false);
        }
        catch {
            setStorageError(true);
        }
    }, [eventsKey, ownedDesktop, paperId, selectionsKey, selectionsLoaded, sourceRevision, state, workspace.owner, workspace.ready]);
    const importLegacy = useCallback(() => {
        if (!ownedDesktop || typeof window === "undefined")
            return false;
        try {
            const storedSelections = safeJsonParse<unknown[]>(window.localStorage.getItem(selectionsKey), []);
            const storedEvents = safeJsonParse<unknown[]>(window.localStorage.getItem(eventsKey), []);
            const legacy = normalizeLoadedSelectionState(paperId, storedSelections, storedEvents);
            dispatch({ type: "merge_remote", state: legacy });
            setLegacyAvailable(false);
            return true;
        }
        catch {
            setStorageError(true);
            return false;
        }
    }, [eventsKey, ownedDesktop, paperId, selectionsKey]);
    const createTempSelection = useCallback((input: {
        paragraph: ReaderParagraph;
        selectedText: string;
        startOffset: number;
        endOffset: number;
    }) => {
        const now = nowIso();
        const selection: ReaderSelection = {
            id: createId("selection"),
            paperId,
            sectionId: input.paragraph.sectionId,
            sectionTitle: input.paragraph.sectionTitle,
            paragraphId: input.paragraph.id,
            blockId: input.paragraph.blockId,
            source: input.paragraph.source,
            pageNumber: input.paragraph.pageStart ?? input.paragraph.source?.pageNumber,
            selectedText: input.selectedText,
            surroundingText: getSurroundingText(input.paragraph.text, input.startOffset, input.endOffset),
            startOffset: input.startOffset,
            endOffset: input.endOffset,
            noteText: "",
            explainQuestion: "",
            explainAnswer: "",
            exampleQuestion: "",
            exampleAnswer: "",
            explained: false,
            exampled: false,
            confused: false,
            createdAt: now,
            updatedAt: now,
        };
        dispatch({ type: "create_temp", selection });
        return selection.id;
    }, [paperId]);
    const discardSelection = useCallback((selectionId: string) => dispatch({ type: "discard", selectionId }), []);
    const discardAllTemp = useCallback(() => dispatch({ type: "discard_all_temp" }), []);
    const updateNote = useCallback((selectionId: string, noteText: string) => dispatch({ type: "update_note", selectionId, noteText }), []);
    const confirmExplain = useCallback((selectionId: string, question: string, answer: string) => dispatch({ type: "confirm_explain", selectionId, question, answer }), []);
    const confirmExample = useCallback((selectionId: string, question: string, answer: string) => dispatch({ type: "confirm_example", selectionId, question, answer }), []);
    const toggleConfused = useCallback((selectionId: string) => dispatch({ type: "toggle_confused", selectionId }), []);
    const mergeRemoteMaterials = useCallback((materials: BackendReaderMaterialSummary) => dispatch({ type: "merge_remote", state: backendMaterialsToSelectionState(materials) }), []);
    return {
        storageError,
        legacyAvailable,
        importLegacy,
        selections: state.selections,
        events: state.events,
        createTempSelection,
        discardSelection,
        discardAllTemp,
        updateNote,
        confirmExplain,
        confirmExample,
        toggleConfused,
        mergeRemoteMaterials,
    };
}
function toResearchSelection(
    selection: ReaderSelection,
    sourceRevision: string,
): ResearchReaderSelection {
    return {
        id: selection.id,
        paperId: selection.paperId,
        sourceRevision,
        sectionId: selection.sectionId,
        paragraphId: selection.paragraphId,
        ...(selection.blockId ? { blockId: selection.blockId } : {}),
        ...(selection.pageNumber ? { pageNumber: selection.pageNumber } : {}),
        selectedText: selection.selectedText,
        surroundingText: selection.surroundingText,
        startOffset: selection.startOffset,
        endOffset: selection.endOffset,
        noteText: selection.noteText.slice(0, 10000),
        explainQuestion: selection.explainQuestion,
        ...(selection.explainAnswer ? { explainAnswer: selection.explainAnswer } : {}),
        exampleQuestion: selection.exampleQuestion,
        ...(selection.exampleAnswer ? { exampleAnswer: selection.exampleAnswer } : {}),
        explained: selection.explained,
        exampled: selection.exampled,
        confused: selection.confused,
        createdAt: Math.max(1, Date.parse(selection.createdAt)),
        updatedAt: Math.max(1, Date.parse(selection.updatedAt)),
    };
}
function fromResearchSelection(selection: ResearchReaderSelection): ReaderSelection {
    return {
        ...selection,
        sectionTitle: selection.sectionId,
        blockId: selection.blockId ?? undefined,
        pageNumber: selection.pageNumber ?? undefined,
        explainAnswer: selection.explainAnswer ?? "",
        exampleAnswer: selection.exampleAnswer ?? "",
        createdAt: new Date(selection.createdAt).toISOString(),
        updatedAt: new Date(selection.updatedAt).toISOString(),
    };
}
function toResearchEvent(event: ReaderEvent, sourceRevision: string): ResearchReaderEvent {
    return {
        id: event.id,
        type: event.type,
        paperId: event.paperId,
        sourceRevision,
        ...(event.selectionId ? { selectionId: event.selectionId } : {}),
        ...(event.sectionId ? { sectionId: event.sectionId } : {}),
        ...(event.paragraphId ? { paragraphId: event.paragraphId } : {}),
        ...(event.pageNumber ? { pageNumber: event.pageNumber } : {}),
        ...(event.selectedText ? { selectedText: event.selectedText.slice(0, 10000) } : {}),
        ...(event.surroundingText ? { surroundingText: event.surroundingText.slice(0, 20000) } : {}),
        createdAt: Math.max(1, Date.parse(event.createdAt)),
    };
}
function fromResearchEvent(event: ResearchReaderEvent): ReaderEvent {
    return {
        id: event.id,
        type: event.type,
        paperId: event.paperId,
        selectionId: event.selectionId ?? undefined,
        paragraphId: event.paragraphId ?? undefined,
        sectionId: event.sectionId ?? undefined,
        pageNumber: event.pageNumber ?? undefined,
        selectedText: event.selectedText ?? undefined,
        surroundingText: event.surroundingText ?? undefined,
        createdAt: new Date(event.createdAt).toISOString(),
    };
}
function backendMaterialsToSelectionState(materials: BackendReaderMaterialSummary): SelectionState {
    const events = materials.events.map(toLocalReaderEvent).filter(isReaderEvent);
    return normalizeLoadedSelectionState(materials.paperId, materials.selections.map((selection) => toLocalReaderSelection(selection, materials.events)), events);
}
function toLocalReaderSelection(selection: BackendReaderSelection, events: BackendReaderEvent[]): ReaderSelection {
    const relatedEvents = events.filter((event) => event.selectionId === selection.selectionId);
    const explanation = lastEventPayload(relatedEvents, "explanation_generated");
    const example = lastEventPayload(relatedEvents, "example_generated");
    const placement = lastEventPayload(relatedEvents, "selection_created")
        ?? lastEventPayload(relatedEvents, "note_updated")
        ?? explanation
        ?? example;
    const fallbackSectionTitle = payloadString(lastEventPayload(relatedEvents, "selection_created"), "sectionTitle")
        ?? payloadString(lastEventPayload(relatedEvents, "note_updated"), "sectionTitle")
        ?? selection.sectionTitle
        ?? selection.sectionId
        ?? "Selection";
    return {
        id: selection.selectionId,
        paperId: selection.paperId,
        sectionId: selection.sectionId ?? selection.target.sectionId ?? "",
        sectionTitle: fallbackSectionTitle,
        paragraphId: selection.paragraphId ?? selection.target.paragraphId ?? "",
        blockId: selection.target.blockId,
        pageNumber: selection.target.pageNumber,
        selectedText: selection.selectedText,
        surroundingText: selection.surroundingText,
        startOffset: payloadNumber(placement, "startOffset") ?? 0,
        endOffset: payloadNumber(placement, "endOffset") ?? selection.selectedText.length,
        noteText: selection.noteText ?? "",
        explainQuestion: selection.explainQuestion ?? payloadString(explanation, "question") ?? payloadString(explanation, "explainQuestion") ?? "",
        explainAnswer: payloadString(explanation, "answer") ?? "",
        exampleQuestion: selection.exampleQuestion ?? payloadString(example, "question") ?? payloadString(example, "exampleQuestion") ?? "",
        exampleAnswer: payloadString(example, "answer") ?? payloadString(example, "example") ?? "",
        explained: selection.explained,
        exampled: selection.exampled,
        confused: selection.confused,
        createdAt: selection.createdAt,
        updatedAt: selection.updatedAt,
    };
}
function toLocalReaderEvent(event: BackendReaderEvent): ReaderEvent | null {
    if (!isLocalReaderEventType(event.type))
        return null;
    return {
        id: event.eventId,
        type: event.type,
        paperId: event.paperId,
        selectionId: event.selectionId,
        paragraphId: event.paragraphId,
        blockId: event.target?.blockId,
        sectionId: event.sectionId,
        pageNumber: event.target?.pageNumber,
        selectedText: event.selectedText,
        surroundingText: event.surroundingText,
        payload: event.payload,
        createdAt: event.createdAt,
    };
}
function lastEventPayload(events: BackendReaderEvent[], type: BackendReaderEvent["type"]) {
    return events.filter((event) => event.type === type).at(-1)?.payload;
}
function payloadString(payload: Record<string, unknown> | undefined, key: string) {
    const value = payload?.[key];
    return typeof value === "string" ? value : undefined;
}
function payloadNumber(payload: Record<string, unknown> | undefined, key: string) {
    const value = payload?.[key];
    return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}
function isLocalReaderEventType(type: string): type is ReaderEventType {
    return [
        "selection_created",
        "selection_discarded",
        "note_updated",
        "explanation_generated",
        "example_generated",
        "confusion_marked",
        "confusion_unmarked",
        "reader_settings_changed",
        "drawer_resized",
        "toc_navigated",
    ].includes(type);
}
function isReaderEvent(event: ReaderEvent | null): event is ReaderEvent {
    return event !== null;
}
function normalizeLoadedSelectionState(paperId: string, selections: unknown[], events: unknown[]): SelectionState {
    const keptSelections = selections
        .filter(isStoredSelection)
        .filter((selection) => selection.paperId === paperId && shouldKeepSelection(selection));
    const keptSelectionIds = new Set(keptSelections.map((selection) => selection.id));
    return {
        selections: keptSelections,
        events: events
            .filter(isStoredEvent)
            .filter((event) => event.paperId === paperId)
            .filter((event) => !event.selectionId || keptSelectionIds.has(event.selectionId)),
    };
}
function isStoredSelection(value: unknown): value is ReaderSelection {
    if (!value || typeof value !== "object")
        return false;
    const selection = value as Partial<ReaderSelection>;
    return typeof selection.id === "string"
        && typeof selection.paperId === "string"
        && typeof selection.sectionId === "string"
        && typeof selection.sectionTitle === "string"
        && typeof selection.paragraphId === "string"
        && typeof selection.selectedText === "string"
        && typeof selection.surroundingText === "string"
        && typeof selection.startOffset === "number"
        && typeof selection.endOffset === "number"
        && typeof selection.noteText === "string"
        && typeof selection.explainQuestion === "string"
        && typeof selection.exampleQuestion === "string"
        && typeof selection.explained === "boolean"
        && typeof selection.exampled === "boolean"
        && typeof selection.confused === "boolean"
        && typeof selection.createdAt === "string"
        && typeof selection.updatedAt === "string";
}
function isStoredEvent(value: unknown): value is ReaderEvent {
    if (!value || typeof value !== "object")
        return false;
    const event = value as Partial<ReaderEvent>;
    return typeof event.id === "string"
        && typeof event.type === "string"
        && typeof event.paperId === "string"
        && typeof event.createdAt === "string"
        && (event.selectionId === undefined || typeof event.selectionId === "string");
}
function selectionReducer(state: SelectionState, action: SelectionAction): SelectionState {
    switch (action.type) {
        case "load":
            return action.state;
        case "merge_remote":
            return mergeSelectionStates(state, action.state);
        case "create_temp":
            return {
                selections: [...state.selections.filter(shouldKeepSelection), action.selection],
                events: [...state.events, createReaderEvent("selection_created", action.selection.paperId, {
                        selectionId: action.selection.id,
                        paragraphId: action.selection.paragraphId,
                        blockId: action.selection.blockId,
                        sectionId: action.selection.sectionId,
                        pageNumber: action.selection.pageNumber,
                        source: action.selection.source,
                        selectedText: action.selection.selectedText,
                        surroundingText: action.selection.surroundingText,
                    })],
            };
        case "discard": {
            const target = state.selections.find((item) => item.id === action.selectionId);
            return {
                selections: state.selections.filter((item) => item.id !== action.selectionId),
                events: target ? [...state.events, createReaderEvent("selection_discarded", target.paperId, {
                        selectionId: target.id,
                        paragraphId: target.paragraphId,
                        blockId: target.blockId,
                        sectionId: target.sectionId,
                        pageNumber: target.pageNumber,
                        source: target.source,
                        selectedText: target.selectedText,
                    })] : state.events,
            };
        }
        case "discard_all_temp": {
            const removed = state.selections.filter((item) => !shouldKeepSelection(item));
            return {
                selections: state.selections.filter(shouldKeepSelection),
                events: [...state.events, ...removed.map((item) => createReaderEvent("selection_discarded", item.paperId, {
                        selectionId: item.id,
                        paragraphId: item.paragraphId,
                        blockId: item.blockId,
                        sectionId: item.sectionId,
                        pageNumber: item.pageNumber,
                        source: item.source,
                        selectedText: item.selectedText,
                    }))],
            };
        }
        case "update_note": {
            const target = state.selections.find((item) => item.id === action.selectionId);
            if (!target || target.noteText === action.noteText)
                return state;
            const updated = { ...target, noteText: action.noteText, updatedAt: nowIso() };
            const nextSelections = shouldKeepSelection(updated)
                ? state.selections.map((item) => item.id === updated.id ? updated : item)
                : state.selections.filter((item) => item.id !== updated.id);
            return {
                selections: nextSelections,
                events: [...state.events, createReaderEvent("note_updated", updated.paperId, {
                        selectionId: updated.id,
                        paragraphId: updated.paragraphId,
                        blockId: updated.blockId,
                        sectionId: updated.sectionId,
                        pageNumber: updated.pageNumber,
                        source: updated.source,
                        selectedText: updated.selectedText,
                        surroundingText: updated.surroundingText,
                        payload: { noteText: action.noteText },
                    })],
            };
        }
        case "confirm_explain":
            return updateSelection(state, action.selectionId, (selection) => ({
                ...selection,
                explained: true,
                explainQuestion: action.question,
                explainAnswer: action.answer,
                updatedAt: nowIso()
            }), "explanation_generated", { question: action.question, answer: action.answer });
        case "confirm_example":
            return updateSelection(state, action.selectionId, (selection) => ({
                ...selection,
                exampled: true,
                exampleQuestion: action.question,
                exampleAnswer: action.answer,
                updatedAt: nowIso()
            }), "example_generated", { question: action.question, answer: action.answer });
        case "toggle_confused": {
            const target = state.selections.find((item) => item.id === action.selectionId);
            if (!target)
                return state;
            const updated = { ...target, confused: !target.confused, updatedAt: nowIso() };
            const nextSelections = updated.confused || shouldKeepSelection(updated)
                ? state.selections.map((item) => item.id === updated.id ? updated : item)
                : state.selections.filter((item) => item.id !== updated.id);
            return {
                selections: nextSelections,
                events: [...state.events, createReaderEvent(updated.confused ? "confusion_marked" : "confusion_unmarked", updated.paperId, {
                        selectionId: updated.id,
                        paragraphId: updated.paragraphId,
                        blockId: updated.blockId,
                        sectionId: updated.sectionId,
                        pageNumber: updated.pageNumber,
                        source: updated.source,
                        selectedText: updated.selectedText,
                        surroundingText: updated.surroundingText,
                    })],
            };
        }
        default:
            return state;
    }
}
function mergeSelectionStates(local: SelectionState, remote: SelectionState): SelectionState {
    const selections = new Map<string, ReaderSelection>();
    for (const selection of remote.selections)
        selections.set(selection.id, selection);
    for (const selection of local.selections)
        selections.set(selection.id, selection);
    const events = new Map<string, ReaderEvent>();
    for (const event of remote.events)
        events.set(event.id, event);
    for (const event of local.events)
        events.set(event.id, event);
    return {
        selections: Array.from(selections.values()),
        events: Array.from(events.values()),
    };
}
function updateSelection(state: SelectionState, selectionId: string, update: (selection: ReaderSelection) => ReaderSelection, eventType: "note_updated" | "explanation_generated" | "example_generated", payload: Record<string, unknown>): SelectionState {
    const target = state.selections.find((item) => item.id === selectionId);
    if (!target)
        return state;
    const updated = update(target);
    return {
        selections: state.selections.map((item) => item.id === selectionId ? updated : item),
        events: [...state.events, createReaderEvent(eventType, updated.paperId, {
                selectionId: updated.id,
                paragraphId: updated.paragraphId,
                blockId: updated.blockId,
                sectionId: updated.sectionId,
                pageNumber: updated.pageNumber,
                source: updated.source,
                selectedText: updated.selectedText,
                surroundingText: updated.surroundingText,
                payload,
            })],
    };
}
export function selectionScope(ownedDesktop: boolean, owner: string | null | undefined, paperId: string, sourceRevision?: string): string {
    return ownedDesktop
        ? `owner:${owner ?? ""}:${paperId}:${sourceRevision ?? ""}`
        : `local:${paperId}`;
}
