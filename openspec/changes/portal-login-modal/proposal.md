## Why

The redesigned research portal has an inert login button and the existing login page is an administrator-oriented username/password flow. Visitors need a consistent in-page account dialog with phone, email, Google, WeChat, and QQ sign-in without losing their research context.

## What Changes

- Add a reusable, accessible login dialog and account control to the redesigned home and papers navigation.
- Support phone/email verification, in-dialog WeChat QR authorization, and Google/QQ provider authorization with a same-page completion path.
- Add real configured delivery/provider integrations, ordinary-user identity provisioning, bounded verification attempts, and server-side sessions. Unconfigured methods remain visibly unavailable, never simulated.
- Keep administrator bootstrap separate and preserve existing session and operator authorization behavior.
- Preserve URL, draft input, filters, and scroll when opening, closing, or completing sign-in; do not automatically submit paid or destructive actions.
- Document provider configuration and separate offline verification from live provider activation.

## Capabilities

### New Capabilities
- `portal-account-access`: In-page account access, multi-provider sign-in, verified ordinary-user identities, and session completion.

### Modified Capabilities

None. Existing administrator bootstrap and protected-operation authorization remain intact.

## Impact

- Frontend account components, redesigned portal navigation, same-origin authentication BFF routes, and targeted tests.
- Authentication application service, identity/challenge persistence, provider/delivery adapters, API routes, and security regression tests.
- Operator-owned third-party credentials, approved redirect domains, SMTP and SMS delivery configuration, and published legal URLs are deployment prerequisites; no external accounts or services are purchased or activated by this change.
- Existing Research/Harness runtime and local-only reading-list semantics are unchanged. Account settings and linking multiple existing accounts are a separate future scope.
