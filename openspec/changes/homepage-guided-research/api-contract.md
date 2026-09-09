# Guided research wire contract

Frontend and backend validate the following shapes. Backend endpoints use `/api/v1/research/guided`; same-origin browser proxies are `/api/research/intent` and `/api/research/search`.

Intent request:
```ts
{ question: string; answers: string[]; previous: ResearchTurn[]; materialIds: string[];
  publicSources: Array<{ id: string; kind: 'paper'|'project'|'discussion'|'note';
    title: string; url: string; notes: string }>;
  skipClarification: boolean; requestedSources?: ('papers'|'projects')[];
  constraints?: { recentYear?: boolean; hasCode?: boolean; paperType?: 'survey';
    language?: 'python'|'typescript'|'javascript'|'rust'|'go';
    license?: 'MIT'|'Apache-2.0'|'BSD-3-Clause'; recentlyActive?: boolean;
    localRunnable?: boolean } }
```
`previous` is at most 19 user-owned turns; use bounded user messages and validated prior result metadata only for resolving follow-ups. Treat all client metadata as untrusted context, never as authority. Explicit initial source constraints will be passed additionally once wired. A caller cannot grant itself access through a material ID. Guest URL/note material handling must avoid rejecting the entire search: accept explicit supported public references separately or return a clear login-required action for private context. `publicSources` is bounded untrusted search context only: the service MUST NOT fetch arbitrary URLs or treat it as authorization. Owned material IDs are resolved against the current account; stored notes, source URL and a bounded excerpt of an already-parsed native paper/PDF document may be included.

Intent response data:
```ts
{ summary: string; query: string; sources: ('papers'|'projects')[];
  constraints: { recentYear?: boolean; hasCode?: boolean; paperType?: 'survey'; language?: 'python'|'typescript'|'javascript'|'rust'|'go'; license?: 'MIT'|'Apache-2.0'|'BSD-3-Clause'; recentlyActive?: boolean; localRunnable?: boolean };
  clarification: null | { question: string; options: string[] };
  confirmationRequired?: boolean; changeNotice?: string;
}
```
Sources max 2 unique. Clarification options 2–3, answer rounds max 2. No confidence/technical labels shown. Never silently strip constraints after follow-ups. Follow-ups may reference a visible result (e.g. second paper).

The application inherits prior conditions omitted by a follow-up candidate. A candidate that changes an existing condition requires confirmation even in automatic mode; an application-derived plain-language change notice names the old and proposed values. Source choices default to the prior choice unless the new request or selection explicitly changes them. Search ports and their DTOs live in `backend/research/ports/guided_research.py`; source adapters depend on these contracts, not the application implementation. HTTP routes consume the interface service boundary.

Search request:
```ts
{ source: 'papers'|'projects'; intent: ResearchIntent; materialIds: string[];
  publicSources: Array<{ id: string; kind: 'paper'|'project'|'discussion'|'note';
    title: string; url: string; notes: string }> }
```
Search response data:
```ts
{ source: 'papers'|'projects'; results: Array<{
  id: string; kind: 'papers'|'projects'; title: string; description: string;
  source: string; url: string; href?: string;
  authors?: string; publishedAt?: string; language?: string; stars?: number;
}>; total: number; moreHref: string }
```
Max 10 results/source; `href` same-origin paper reader/detail or project detail only; `url` HTTP(S) public source without credentials. Failures use existing success:false/error envelope with user-understandable Chinese text. Each source independent for partial success and retry.

Paper search reads the same verified public cache as the paper UI: explicit `NEWSROOM_PAPERS_DATA_PATH`, otherwise `.newsroom/papers/arxiv-papers.json` followed by `frontend/data/papers/arxiv-papers.json`. Cache results disclose collection date. Recent-year filters use the past 365 days; more-results URLs preserve query, date, code and survey filters. The browser proxy only adds native-reader actions for an existing compiled artifact whose gate passed.

Project search uses Project Radar when available; empty/unconfigured Radar falls back to the existing bounded public GitHub connector. A local-setup request always uses this README-backed connector because legacy Radar profiles derive local_deployable from a repository URL alone. API failures are distinct from an empty match. Local-setup filtering reads at most ten repositories and three fixed README paths each, within a 30-second admission budget plus the final bounded fetch. It requires both installation and launch commands; no repository code is executed, and results disclose the README check.

The mirrored history schemas are `frontend/src/lib/research/conversation.ts` and `interfaces/services/research_conversation_model.py`. A conversation retains at most 20 turns and each turn can snapshot its selected material IDs. Legacy turns without this optional field use conversation-level context.

PDF uploads return a durable received identity before conversion. Conversion retries serialize per import and retain ownership. Completion requires a parsed document and a passing reading-quality report. Research text conversion alone does not imply a published full-text reader artifact; no reader link is invented for an import.
Snapshot persistence compares canonical model representations while retaining the original immutable observation; equivalent actor-scope normalization cannot make a retry conflict, and changed source content remains rejected.

`moreHref` is optional. A project continuation is only offered when the destination supports every applicable confirmed filter; otherwise direct source links and in-conversation follow-ups remain available. Explicit null intent constraints propose clearing a previous condition and require visible confirmation; omitted fields retain the previous choice.
