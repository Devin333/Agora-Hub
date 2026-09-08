## ADDED Requirements

### Requirement: Redesigned method and task directories
The system SHALL replace the legacy method and task index layouts with a shared desktop directory matching the paper discovery visual system. It SHALL provide direction filtering, category search, sorting, URL-backed category detail and real associated paper reading links.

#### Scenario: Unannotated category is browsed
- **WHEN** a user opens a category without verified paper references
- **THEN** its definition is available and its association status is explicit; no example papers, counts or relationships are presented as live data

#### Scenario: Directory context survives detail navigation
- **WHEN** a user selects a category and returns to the directory or refreshes the URL
- **THEN** direction, search and sort context remain available and category detail is recoverable

### Requirement: Progressive category navigation
The design paper sidebar SHALL show topic, method and task groups with five category entries each by default, an explicit remaining-count expansion control and an independent group collapse control.

#### Scenario: Browse additional categories
- **WHEN** a group contains more than five categories
- **THEN** its first five are visible and the user can expand or collapse the remainder without navigating away

#### Scenario: Keep a selected category visible
- **WHEN** a category outside the first five is selected and the list is collapsed
- **THEN** the selected category replaces the fifth visible entry and remains removable

### Requirement: Factual category availability
The sidebar SHALL calculate counts only from published paper associations, deduplicate repeated references, retain existing taxonomy names without fabricated counts, and distinguish unannotated categories.

#### Scenario: Taxonomy exists without associated papers
- **WHEN** a known category has no verified public-paper association
- **THEN** it shows a pending annotation state and does not offer a misleading active filter

### Requirement: Continuous combined filtering
Method and task filters SHALL combine with existing query, topic, dates and feature filters in both API requests and client fallback, persist in URLs and expose individually removable selection chips.

#### Scenario: Change a method filter
- **WHEN** the reader selects a method with other filters active
- **THEN** only the method changes, the page and preview reset, and all remaining query context is preserved

#### Scenario: API failure
- **WHEN** paper requests fail with a method/task selected
- **THEN** local public-paper filtering applies the same method/task constraints

#### Scenario: Clear selection
- **WHEN** the reader removes one selection chip or uses reset
- **THEN** that dimension or all filter dimensions respectively are cleared without losing the research question

### Requirement: Accessible desktop categories
The sidebar SHALL expose expansion state and keyboard actions, keep controls reachable in a long desktop sidebar and retain separate method/task directory links.

#### Scenario: Use keyboard navigation
- **WHEN** a reader focuses a group heading or more button and presses Enter
- **THEN** the corresponding content changes with accurate aria-expanded and visible focus
