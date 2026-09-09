## ADDED Requirements

### Requirement: Context menu preserves the question
The homepage SHALL place an accessible add menu at the leftmost position of the input. It SHALL expose PDF upload, supported source links, explicit material selection and a storage group. Added context SHALL have removable visible labels and SHALL NOT replace the question or implicitly attach a whole group.

#### Scenario: Add a link while typing
- **WHEN** a user adds a supported source link after writing a question
- **THEN** the question remains intact and a removable source reference appears

#### Scenario: Upload needs authentication
- **WHEN** a guest starts a private PDF import
- **THEN** the existing login dialog opens while preserving the question and selected context

#### Scenario: Text-bearing PDF becomes research context
- **WHEN** a signed-in user imports a PDF with a usable text layer
- **THEN** a bounded in-process parser can preserve source-located text beyond the abstract and mark completion only after document-quality validation
- **AND** scanned, encrypted or rejected documents report a clear non-success state, while research text extraction does not grant a formal compiled-reader link

### Requirement: Plain language bounded clarification
The system SHALL distinguish automatic and plan execution. It SHALL ask at most two clarification rounds, one question per round, only for missing information that changes the search. Users SHALL be able to provide free text, skip to search, edit the confirmation and choose papers, projects or both. Selecting a choice SHALL NOT execute until the user starts the search in plan mode.

#### Scenario: Broad request
- **WHEN** a user asks about Agent without a clear purpose
- **THEN** a plain-language question offers a few understandable choices and a way to start without answering

#### Scenario: Explicit paper request
- **WHEN** the user already asks for papers
- **THEN** the system does not repeat the paper-versus-project question

### Requirement: Real bounded search and truthful progress
The application SHALL validate intent candidates, enforce allowed data sources, constraints and finite request budgets, and query real paper and project services. Visible progress SHALL correspond to actual request lifecycle events. Cancellation, failures and stale responses SHALL NOT be represented as successful searches.

#### Scenario: Both content types
- **WHEN** the user confirms both papers and projects
- **THEN** both services are queried and their source-backed results are presented separately in the current research

#### Scenario: One source fails
- **WHEN** papers return but projects fail or time out
- **THEN** papers remain visible, the project failure is explained and retry does not create a new research record

#### Scenario: Stop or account switch
- **WHEN** the user stops, starts another research, or changes accounts during a request
- **THEN** its late response cannot modify the newly active research or account

### Requirement: Usable sourced results and follow-ups
Each result SHALL have a title, source-backed description/metadata and valid direct actions. Full-text reader actions SHALL use the native reader when ready. Users SHALL be able to continue asking within the same research, including changing constraints and referring to a displayed result. Conditions SHALL NOT be silently relaxed.

#### Scenario: Open a result and return
- **WHEN** a user opens a paper reader or project and returns
- **THEN** the original research, results, context and viewing position can be restored

#### Scenario: No matching result
- **WHEN** a source returns no matching records
- **THEN** the user sees an honest empty state with edit or explicit relaxation actions

### Requirement: Owned conversation continuity
The system SHALL store bounded conversation turns, confirmations, result references, context and durable phase events in the existing owned history. Follow-ups SHALL remain in one visit. Old snapshots SHALL remain readable; private context SHALL be validated by owner and never inferred from client IDs alone.

#### Scenario: Restore an interrupted plan
- **WHEN** a user reloads or resumes a saved research before confirmation or during a request
- **THEN** the saved content appears with an explicit continue/retry action and no hidden automatic request

#### Scenario: Guest and account isolation
- **WHEN** the active owner changes
- **THEN** the previous owner's conversation and attachments are not rendered or silently imported

### Requirement: Desktop layout and existing module boundaries
The idle homepage SHALL retain its purple glass styling, centered composer, 2x2 module cards and collapsible history sidebar. Active research SHALL present readable conversation/results and a continuing input, with keyboard access and stable focus. Community SHALL keep its module entry and reports SHALL use the existing editable preparation flow.

#### Scenario: Start and reset research
- **WHEN** the user starts a research and later chooses new research
- **THEN** the UI transitions from idle to conversation and back without losing saved history

### Requirement: Verified delivery
Delivery SHALL include focused service/schema/UI tests, real desktop browser evidence for paper and mixed search workflows, strict OpenSpec validation, required smoke, scoped commits and a usable local frontend.

#### Scenario: Acceptance audit
- **WHEN** completion is claimed
- **THEN** every requirement has current code and appropriately scoped verification evidence, with external limitations stated explicitly
