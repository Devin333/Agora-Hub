## ADDED Requirements

### Requirement: Independent discoverable filtering
The preview SHALL combine keyword, topic, feature, and date filters independently, preserve them in the URL, offer removable active filters, and default searched results to deterministic keyword relevance.

#### Scenario: Topic refines a keyword query
- **WHEN** a user searches Agent and selects cs.AI
- **THEN** both q=Agent and topic=cs.AI remain active and all returned papers satisfy both criteria

#### Scenario: Date range survives reload
- **WHEN** a valid from/to range is applied and the page reloads
- **THEN** the same inclusive range and matching results are restored

### Requirement: Usable local workspace
The preview SHALL expose saved papers and a factual comparison view, persistent counts, removal, loading, retry, and empty states without fabricated metadata.

#### Scenario: Revisit saved selections
- **WHEN** the user saves a paper, reloads, and opens the reading list
- **THEN** the paper remains available and previously stored later IDs remain accessible

#### Scenario: Compare selected papers
- **WHEN** at least two available papers are selected
- **THEN** the comparison view displays their real source metadata side by side and missing fields as unavailable

### Requirement: Reader continuity
Title and cover actions SHALL consistently open preview; reader entry SHALL preserve discovery filters, page, selected view, and best-effort scroll through an allowlisted local return path.

#### Scenario: Return from reading
- **WHEN** a user reads a paper from page two of filtered discovery and returns
- **THEN** the original filtered page is restored instead of redirecting to legacy /papers

#### Scenario: Reject an external return path
- **WHEN** returnTo contains an external or protocol-relative URL
- **THEN** the reader uses /papers as the safe fallback

### Requirement: Honest research context
The home paper entry SHALL retain the original user question and the destination SHALL identify its search as keyword matching, not semantic AI retrieval.

#### Scenario: Home question reaches discovery
- **WHEN** a user submits a paper question and enters the suggested module
- **THEN** the original question is visible in discovery and can be edited into a keyword search

### Requirement: Readable compact discovery
The preview SHALL retain the accepted glass styling, make read and save primary actions, localize known topic codes, and label category count scope. At 1366x900 the first viewport SHALL show two paper titles and abstracts without horizontal overflow.

#### Scenario: Desktop scanning
- **WHEN** the catalog opens at 1366x900
- **THEN** two real paper summaries are visible and actions are keyboard accessible with readable labels

### Requirement: Time-aware data status
The preview SHALL distinguish source/collection status from latest available publication date, preserve other filters when clearing time, and provide accurate empty-state recovery.

#### Scenario: Time filter beyond available publications
- **WHEN** the selected period starts after the latest available publication and returns no papers
- **THEN** the page explains the available publication cutoff and offers all-time recovery without clearing the query or topic
