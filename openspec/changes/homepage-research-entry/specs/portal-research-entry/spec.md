## ADDED Requirements

### Requirement: Intent-aware direct entry
The homepage SHALL default to automatic mode, respect manual selection, directly navigate on clear intent, and offer compact choices for ambiguous or unknown intent.

#### Scenario: Project question includes research
- **WHEN** a user submits a question about an open-source research project
- **THEN** it opens projects rather than matching the generic word research to papers

#### Scenario: Manual selection and ambiguity
- **WHEN** the user selects a module or submits a question with multiple equally plausible destinations
- **THEN** manual choice takes precedence, or a compact choice is shown without a separate analysis panel

### Requirement: Real module context
All four modules SHALL accept the original question; papers, projects and community SHALL apply editable queries to real data; reports SHALL offer a prefilled preparation page with editable topic, source materials and notes, explicit local draft saving and continued editing. AI generation is explicitly deferred by the user and SHALL NOT be simulated.

#### Scenario: Cross-module submission
- **WHEN** a homepage question is sent to any module
- **THEN** the destination preserves and displays the question and performs the matching search or prepares the report task

#### Scenario: Upstream error
- **WHEN** a data or task API fails
- **THEN** input remains recoverable and an error with retry is shown without fabricated data or success

### Requirement: Local research continuity
The homepage SHALL show one resume row only when real browser history exists, provide recent-history management, and restore original questions, selected module, safe filter URLs and scroll position. History SHALL be bounded, validated and identified as local browser data.

#### Scenario: Resume after filtering
- **WHEN** a user searches, changes a filter and scrolls, then resumes from the homepage
- **THEN** the module, question, filter and scroll position are restored after content loads

#### Scenario: New user and storage restrictions
- **WHEN** there is no history or storage is blocked or malformed
- **THEN** no fabricated resume row appears and normal navigation remains usable

#### Scenario: Delete records
- **WHEN** a user deletes one record or clears history
- **THEN** the recent menu and resume row reflect the remaining records

### Requirement: Desktop homepage interaction
The homepage SHALL retain Ask. Discover., the centered composer, purple glass styling and centered 2x2 module cards, with benefit descriptions and mode-specific examples. Menus SHALL support keyboard, Escape and outside dismissal; examples and start-research SHALL focus the input; empty and composing input SHALL not submit.

#### Scenario: Editing and navigation feedback
- **WHEN** the user chooses an example, types with a Chinese IME, submits or returns from a module or login
- **THEN** examples fill without submitting, composition Enter does not send, genuine pending navigation is announced, and drafts survive the return

#### Scenario: Desktop layout
- **WHEN** the page is viewed at 1366x768 or 1440x900
- **THEN** the composer remains centered horizontally and vertically and the four centered module cards follow below in two columns
