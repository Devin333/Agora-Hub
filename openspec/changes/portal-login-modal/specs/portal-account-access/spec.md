## ADDED Requirements

### Requirement: In-page account dialog
The portal SHALL open one accessible login dialog from home and paper navigation without replacing the current page. The dialog SHALL retain research drafts, filters, and scroll on close or successful login, and SHALL restore focus to its trigger.

#### Scenario: Close while researching
- **WHEN** a visitor opens login from a filtered paper list and presses Escape
- **THEN** the dialog closes and the unchanged list, URL, draft, scroll, and trigger focus remain available

### Requirement: Discoverable sign-in methods
The dialog SHALL expose phone and email tabs and Google, WeChat, and QQ entries. It SHALL use configured capability status and SHALL NOT display fake QR codes, fake sent-code success, or simulated authenticated users.

#### Scenario: Provider is unconfigured
- **WHEN** a visitor selects an unavailable sign-in method
- **THEN** the interface explains the unavailability without issuing a provider authorization or verification request

### Requirement: Verified ordinary-user registration
Phone/email verification and provider authorization SHALL resolve a verified provider-qualified identity and atomically create or reuse a non-privileged ordinary account. They MUST NOT bootstrap an administrator or merge accounts merely because an email or display name matches.

#### Scenario: Repeat provider login
- **WHEN** the same verified provider subject completes login more than once
- **THEN** it receives the same ordinary account, and a different provider subject cannot take over that account

### Requirement: Bounded one-time verification
Verification challenges SHALL be bound to browser and destination, use expiring non-plaintext code proofs, limit sending and guessing, and consume successful verification atomically. Only successfully delivered messages SHALL produce a sent state.

#### Scenario: Replay or destination change
- **WHEN** a code is reused after consumption or submitted with a different browser or destination
- **THEN** verification fails and no session is issued

#### Scenario: Delivery failure or rate limit
- **WHEN** delivery fails or the configured sending/attempt budget is exhausted
- **THEN** a stable error is returned and the UI offers only a bounded, valid retry path

### Requirement: Provider authorization state and callbacks
OAuth authorization SHALL use real registered provider endpoints and short-lived browser-bound state. The BFF SHALL reject unexpected origins and callback state, keep credentials/tokens server-side, and only notify the initiating page through a validated same-origin completion message.

#### Scenario: Forged completion or replay
- **WHEN** a foreign window sends a completion message or a consumed state is replayed
- **THEN** the page does not authenticate from that message and the backend refuses replay

#### Scenario: WeChat QR and cancelled popup
- **WHEN** WeChat is selected or an external provider window is cancelled
- **THEN** WeChat uses the official embedded QR flow and cancellation returns to a usable in-page method selection without losing research context

### Requirement: Session and deployment boundaries
Successful login SHALL set the existing HttpOnly session cookie and remove raw session tokens from browser-readable responses. Public methods SHALL require valid provider/delivery configuration and published legal URLs, preserve existing privileged-route authorization, and document activation prerequisites separately from offline checks.

#### Scenario: Ordinary user requests operator action
- **WHEN** an account created through a public login method requests an administrator-only operation
- **THEN** the existing authorization checks deny that operation
