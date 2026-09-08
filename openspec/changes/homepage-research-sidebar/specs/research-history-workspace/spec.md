## ADDED Requirements

### Requirement: Desktop research workspace navigation
The homepage SHALL display a collapsible left history sidebar and center the composer in the remaining workspace with four module tiles below in two columns. Sidebar history SHALL replace duplicate dropdown and resume-strip navigation and support keyboard interaction.

#### Scenario: Collapse navigation
- **WHEN** a user collapses or expands the sidebar
- **THEN** the usable content area resizes without horizontal overflow and the composer remains centered in that area

### Requirement: Organized research history
Users SHALL search history, filter favorites, manage one-level groups, rename and move records, and delete with undo. Removing a group SHALL ungroup its records without deleting them. Recent records SHALL be organized by local calendar date.

#### Scenario: Group deletion preserves research
- **WHEN** a user confirms deleting a group
- **THEN** its research remains available in recent history and has no group assignment

#### Scenario: Recover a deleted record
- **WHEN** a user deletes a research record and chooses undo
- **THEN** the original record, title, favorite status and valid group assignment are restored

### Requirement: Stable research sessions and migration
Each new homepage research SHALL receive an independent session ID. Navigation and filter changes within that research SHALL update its resume URL and position without duplicating the record. Existing local records SHALL migrate without losing questions or resume state.

#### Scenario: Repeat the same question
- **WHEN** a user starts two new research sessions with identical questions
- **THEN** both sessions remain independently accessible and filter changes only update the corresponding session

### Requirement: Account-owned durable history
Authenticated history SHALL persist through an account-authorized service using the authenticated user as owner. Guest history SHALL remain local and only import after explicit user action. Signing out or switching accounts SHALL remove previous account data from the visible workspace.

#### Scenario: Account isolation
- **WHEN** another account requests its history or provides a foreign record identifier
- **THEN** the response contains only its own workspace and cannot alter the first account's data

#### Scenario: Explicit guest import
- **WHEN** a user elects to merge local history after login
- **THEN** guest records and groups are imported once by identity without overwriting existing account records or deleting the guest copy

### Requirement: Honest persistence and concurrent changes
The UI SHALL distinguish local saving, account synchronization and failures. The account service SHALL reject stale revisions atomically; the client SHALL retain unsaved edits and offer a recovery path without silent overwrites.

#### Scenario: Concurrent account edits
- **WHEN** two clients write based on the same revision
- **THEN** only one write succeeds and the other receives a conflict with an explicit reload or retry path

#### Scenario: Unavailable storage
- **WHEN** local or account persistence fails
- **THEN** the UI does not claim success and research navigation remains available
