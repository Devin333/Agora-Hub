# Agora Hub Reader Portal

Reader-facing Agora Hub experience for news, research papers, and projects. This app is separate from `apps/web`, which remains the operational console.

## Getting Started

Install and run with pnpm through Corepack:

```bash
corepack pnpm install
corepack pnpm dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

Useful checks:

```bash
corepack pnpm lint
corepack pnpm typecheck
corepack pnpm build
```

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.

## Paper Agent production-profile E2E

Run the bounded Paper Agent acceptance entry from `frontend`:

```bash
npm run test:e2e:paper-agent
```

This entry is intentionally separate from `npm run test:e2e`. It selects only `tests/e2e/paper-agent-app.spec.ts`, `tests/e2e/paper-agent-settings.spec.ts`, and `tests/e2e/research-readiness.spec.ts`, runs one worker, and forces the `hosted` profile over plain HTTP on loopback (`NEWSROOM_ALLOW_INSECURE_ORIGIN=1`; production requires HTTPS). The browser and production server share the fixed public origin `http://127.0.0.1:3317`; backend-facing configuration is pinned to the non-production loopback endpoint `http://127.0.0.1:8317`.

The web-server wrapper copies the frontend source into an isolated temporary workspace, excludes local paper data and environment files, links the installed dependencies, then runs a real `next build --webpack` followed by `next start`. Its child environment is allowlisted and contains only fixed E2E runtime identity and loopback settings, so repository-root provider credentials and user research material are not loaded by the production-profile server. Browser API behavior remains supplied by the existing Playwright fixtures inside the selected specs; production components and the general Playwright configuration are not mocked or changed.

This command is the Paper Agent acceptance entry only. It does not replace, rewrite, or imply a passing result for the mixed admin/default/desktop suite behind `npm run test:e2e`.
