## ADDED Requirements

### Requirement: Group-aware research entry
The homepage SHALL display a removable active group and SHALL save newly started research into that group. Group destination and material context MUST be separate explicit choices.

#### Scenario: Research inside a group
- **WHEN** a user selects a group and submits a new question
- **THEN** the composer names the destination and the resulting history record belongs to it
- **AND** group documents are not implicitly treated as answer evidence

### Requirement: Applied search constraints
The homepage SHALL expose mode-relevant paper and project conditions, serialize them into destination URLs and display editable recognized conditions. Result pages MUST apply every offered condition to real source-backed data and show active filters.

#### Scenario: Code-bearing papers from the last year
- **WHEN** a user selects last-year and has-code conditions for a paper query
- **THEN** the destination shows those conditions and excludes papers outside the date range or lacking code

### Requirement: Exact research continuation
History SHALL retain a safe typed last activity for paper reading and report preparation, while keeping the original discovery query and filters. A saved chapter or PDF page MUST resume only for the correct owner and paper; an unavailable target MUST have a clear fallback.

#### Scenario: Resume paper chapter
- **WHEN** a user returns to a study after reading a chapter
- **THEN** its history shows the paper/chapter and opens the same chapter without requiring another search

#### Scenario: Resume report draft
- **WHEN** a user selects a report preparation history entry
- **THEN** the exact owned draft and materials reopen with accurate save/sync state

### Requirement: Explicit navigation and search scopes
History search SHALL default to all records and SHALL visibly indicate any group constraint. Ctrl/Cmd+K SHALL open a keyboard-accessible search/action palette across the research modules. Favorites SHALL offer direct expandable shortcuts; archive SHALL be reversible and archived content SHALL remain discoverable.

#### Scenario: No match within group
- **WHEN** a group-scoped query matches only records in another group
- **THEN** the empty state identifies the current scope and offers all-history search

### Requirement: Drafts and reusable prompts
The homepage SHALL distinguish a restored unsent draft from a submitted question, support clear/continue actions and allow users to save/reuse/delete named prompt templates with their mode and constraints. Account changes MUST not reveal another account's drafts or templates.

#### Scenario: Reuse prompt
- **WHEN** a user chooses a saved prompt
- **THEN** its text, mode and constraints populate an editable unsent composer

### Requirement: Source reference and PDF entry
The homepage SHALL recognize arXiv, DOI and GitHub references and provide real source-appropriate reading, related-search and grouping actions. It SHALL accept PDF uploads with authenticated ownership, actual parse status, errors and bounded retry. Reader content MUST come from validated compiled source artifacts.

#### Scenario: Upload and read
- **WHEN** an authenticated user uploads a supported PDF
- **THEN** the system records an owned import, reports actual conversion state and exposes the native reader only when the source conversion gate passes
- **AND** failures retain actionable retry/status without rendering an abstract or generated prose as the full paper

### Requirement: Group material workspace
Groups SHALL collect source references, notes and report drafts under the current owner. Users SHALL select materials for source-backed comparison, explicit research context and report preparation without copying links manually.

#### Scenario: Reuse selected materials
- **WHEN** a user selects two saved papers and chooses comparison or report preparation
- **THEN** the selected source identities, known attributes and notes are preserved in the corresponding destination
- **AND** missing attributes are identified rather than invented

### Requirement: Ownership and persistence continuity
Research history, materials, drafts and prompts SHALL be isolated by authenticated account with versioned atomic persistence and explicit guest import. The UI MUST distinguish local-only data, pending edits, synced data and conflicts.

#### Scenario: Account switch with pending work
- **WHEN** the active account changes while a write or restoration is pending
- **THEN** stale responses and callbacks cannot modify or reveal the new account workspace

### Requirement: Desktop visual continuity
The homepage SHALL retain the approved purple glass style, centered composer and 2x2 modules below it. New controls MUST use progressive disclosure, readable consistent typography and keyboard-visible focus; formal paper reading SHALL remain full-screen.

#### Scenario: Desktop sidebar collapse
- **WHEN** the sidebar expands or collapses
- **THEN** the composer remains centered within the remaining desktop workspace and no horizontal overflow occurs at supported desktop sizes
