## ADDED Requirements

### Requirement: Native chapter reading is primary
The workspace SHALL open chapter reading by default and reconstruct the complete supported source into its own typed blocks, rather than embedding upstream HTML or opening a PDF as the primary view.

#### Scenario: Read the complete supported paper
- **WHEN** a published paper has a verified arXiv LaTeXML source
- **THEN** chapter reading displays original sections, paragraphs, equations, tables, all figure panels, references and appendices in source order

#### Scenario: Optional PDF comparison
- **WHEN** a user explicitly opens PDF comparison and later reloads
- **THEN** the PDF page is saved per paper but chapter reading is again the default view

### Requirement: Independent source publication gate
The workspace SHALL verify title identity, substantive body text, structural coverage and actual image assets independently of AI analysis. Analysis text, abstracts and retrieval sections MUST NOT substitute for missing full text.

#### Scenario: Source conversion unavailable
- **WHEN** source retrieval, conversion or completeness validation fails
- **THEN** chapter reading shows an unavailable or review state with retry and source actions without silently substituting an abstract or PDF

#### Scenario: Safe source assets
- **WHEN** a source figure is published
- **THEN** its assets have verified content, dimensions, checksum and same-paper source provenance, and the asset endpoint cannot fetch arbitrary URLs

#### Scenario: HTML source provenance
- **WHEN** a block originates in structured HTML rather than PDF coordinates
- **THEN** it exposes the real source locator without fabricated PDF page numbers or bounding boxes

### Requirement: Coherent desktop reading workspace
The workspace SHALL provide outline navigation, readable native article text, source-preserving visual rendering, auxiliary notes, focus mode and font controls consistent with Agora.

#### Scenario: Navigate beyond initially rendered sections
- **WHEN** a reader selects a later outline entry or inline reference
- **THEN** the target content is rendered and scrolled into view

#### Scenario: Focus on reading
- **WHEN** focus mode is enabled
- **THEN** side panels hide and can be restored without replacing the article

### Requirement: Grounded assistance and durable local notes
The workspace SHALL separate real AI answers from source text and preserve per-paper local notes without claiming cloud synchronization.

#### Scenario: AI unavailable
- **WHEN** native full text is ready but AI analysis is not
- **THEN** full text and notes remain available while question drafts can be retained and submission is disabled

#### Scenario: Cancellable grounded questions
- **WHEN** a configured reader submits a question
- **THEN** it shows a cancellable pending state followed by actual returned evidence or a recoverable error with the question retained

#### Scenario: Continue notes
- **WHEN** the same paper is reopened
- **THEN** its notes, question draft, font size and reading position are restored and remain isolated from other papers

#### Scenario: Storage failure
- **WHEN** a browser write fails
- **THEN** notes remain editable with a clear unsaved state and Markdown export

### Requirement: Discovery continuity
The workspace SHALL preserve safe discovery query state and retain the existing formal reader entry.

#### Scenario: Return to filtered design papers
- **WHEN** a reader follows the back control
- **THEN** the original safe filtered design-list URL is restored

#### Scenario: Formal reader entry
- **WHEN** a user opens a paper from the formal list
- **THEN** the existing formal reader route remains in use
