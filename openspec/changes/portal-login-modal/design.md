## Context

The Next.js 14 portal already uses Radix, same-origin BFF routes, and HttpOnly session cookies. The Python application service owns username/password authentication and file-locked user/session records. Home and paper discovery retain state in mounted components and URLs. The public account dialog must not reuse administrator bootstrap as registration.

## Goals / Non-Goals

**Goals:**
- A compact desktop dialog consistent with the accepted lavender/glass visual language.
- Configured phone/email verification and Google/WeChat/QQ authorization, with explicit unavailable/error states.
- Durable, bounded one-time challenges and provider state; ordinary identities cannot acquire privileged roles.
- Safe session creation and a return path that preserves the current page.

**Non-Goals:**
- No mobile-specific redesign, new landing page, social account purchases, provider approval, or live SMS/email sending during automated checks.
- No automatic account merging, account-settings UI, cloud reading-list migration, or alteration of Harness behavior.
- No invented legal text. Deployment supplies published terms/privacy URLs before public registration is enabled.

## Decisions

### Keep the portal mounted

Use a shared account provider and Radix dialog rather than route navigation or multiple nested dialogs. Home/papers navigation opens the same dialog. Focus is trapped and restored, Escape and close dismiss, background scrolling is locked, and submissions are abortable. Tabs retain independent phone/email drafts, but a challenge remains bound to its original destination. The initial view contains phone/email tabs, verification controls, and branded Google/WeChat/QQ controls.

### Keep protocol credentials server-side

The frontend BFF issues a random HttpOnly browser-binding cookie. Public challenge and authorization endpoints bind transactions to this browser proof. Authorization uses short-lived, single-use state; callbacks validate the state and browser binding before exchanging the provider code. Google and QQ use official authorization windows; WeChat embeds the official QR view and completes through a same-origin callback. Callback messages never contain session tokens and must match both expected origin and expected window before the parent re-fetches session state. A blocked popup has an explicit redirect fallback with a validated local return path.

WeChat's cross-site iframe navigation initially omits SameSite=Lax cookies. Its callback GET therefore serves a CSP-restricted same-origin document that POSTs the code and state to the callback before any exchange. That POST must match the browser-facing origin and the original HttpOnly state/binding cookies. This preserves SameSite=Lax and avoids moving session tokens into browser-readable messages.

### Preserve the existing session contract

The Python service returns the existing session model; the BFF removes the token from response JSON and sets the existing HttpOnly cookie. New identities use a non-privileged role. Existing user records and password login remain readable. Subject identity is provider-qualified and never inferred from an unverified profile email. Administrator bootstrap remains separate and cannot be invoked from the modal.

### Fail closed when providers are unavailable

Public capabilities expose only availability and published legal URLs, never credentials. Real provider/delivery adapters are selected from operator configuration; no fake success, console OTP, placeholder QR, or frontend-only session is permitted. Missing configuration produces an explanatory unavailable state without sending a request. Missing legal URLs prevent new public account flows from starting.

### Bound verification and delivery

Persist one-time verification challenges using the existing locked storage pattern, store only keyed code digests, expire challenges, cap attempts, atomically consume successful proofs, and rate-limit sending/verification across browser and destination. Failed delivery does not report success. Provider timeouts and rejection have stable public errors without revealing secrets or raw upstream responses. Browser binding and same-origin BFF checks prevent login CSRF; challenge identity matching prevents switching a verified code to a different destination.

## Risks / Trade-offs

- Provider approval and real credentials are external prerequisites -> code and mocked-provider contract tests can be completed offline, but activation/live verification must be reported separately.
- File storage is a single-host deployment boundary -> reuse atomic locking, document shared-storage limitations, and do not claim distributed rate limits.
- Browser popup policies and embedded provider constraints differ -> provide cancellation, expiry, retry, and explicit redirect fallback; test callback-origin/window validation.
- Existing API mock mode can fabricate data -> public auth methods bypass generic mock resolution and use the real BFF.

## Migration Plan

Deploy backend and frontend together with methods disabled until configured. Initialize the administrator before enabling public identity creation. Set the documented callback origin, provider credentials, delivery services, challenge secret, and legal URLs. Run provider-specific live acceptance only with operator approval. Rollback disables provider configuration and retains existing users/sessions; never delete authentication records.

## Open Questions

Operator-owned provider applications, SMTP/SMS service credentials, and legal URLs have not been supplied. These remain deployment inputs rather than invented implementation values.
