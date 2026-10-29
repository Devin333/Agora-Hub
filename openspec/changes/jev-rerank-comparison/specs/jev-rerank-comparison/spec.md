## ADDED Requirements

### Requirement: Reproducible fixed-input pilot

The experiment SHALL freeze real-corpus candidates, reviewed question labels, seed, sampling policy, source hashes, and candidate order in a versioned manifest. It SHALL select candidates without consulting gold labels, exclude abstention cases from ranking metrics with an explicit count, and report gold absence without injecting evidence.

#### Scenario: Gold falls outside candidate shortlist
- **WHEN** lexical selection does not retrieve a labeled gold chunk
- **THEN** the manifest preserves that miss and every arm evaluates the same shortlist

### Requirement: Isolated real-provider execution

The experiment SHALL run existing lexical and BGE implementations and an opt-in real Jev API arm using identical candidate text. It SHALL exclude labels from provider requests, bound request counts and timeouts, validate scores and response identities, and keep all production routing and gates unchanged.

#### Scenario: Provider returns a partial or malformed response
- **WHEN** Jev omits a question or returns invalid score data
- **THEN** the affected case is recorded as failed without borrowing baseline scores

### Requirement: Honest comparable measurement

Reports SHALL bind dataset fingerprints and settings, preserve per-case observations, separate cold start and warmup from measured query latency, count all observed API usage, report total coverage and paired quality metrics, and distinguish estimated API charges from unknown local compute cost. Comparison SHALL reject mismatched manifests and SHALL NOT declare a winner when an arm is unmeasured.

#### Scenario: Jev credentials are absent
- **WHEN** the baseline finishes without a configured Jev key
- **THEN** the report contains baseline results and explicitly marks Jev as unmeasured rather than fabricating a comparison

#### Scenario: Reports use different input snapshots
- **WHEN** a summary is requested for different dataset fingerprints
- **THEN** the summary fails with a clear mismatch error

### Requirement: Credential confidentiality

The experiment SHALL load credentials from environment or a local env file and SHALL NOT persist authorization headers, keys, or unsanitized provider error bodies.

#### Scenario: Authentication fails
- **WHEN** the provider rejects a credential
- **THEN** the report records the status without including the credential or response body
