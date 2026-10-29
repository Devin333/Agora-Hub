# The hosted web client and BFF: a Next standalone server behind Caddy.
# Build from the repository root:
#   docker build -f deploy/hosted/web.Dockerfile -t agora-web .

FROM node:24-bookworm-slim AS build
WORKDIR /src/frontend
ENV NEXT_TELEMETRY_DISABLED=1
# The lockfile's resolved URLs point at the public registry; npm rewrites them
# to this one, so a mirror can be used where the public registry is blocked.
ARG NPM_REGISTRY=https://registry.npmjs.org/
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund --registry "$NPM_REGISTRY"
COPY frontend ./
# The profile is baked into the client bundle; the public origin and API
# address are read at request time from the runtime environment.
RUN NEWSROOM_DEPLOYMENT_PROFILE=hosted npm run build

FROM node:24-bookworm-slim
# The release this image was published as. The desktop app compares it with
# the release it expects before it opens a stack it manages on this computer.
ARG AGORA_IMAGE_TAG=dev
ENV AGORA_IMAGE_TAG=$AGORA_IMAGE_TAG
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    NEWSROOM_DEPLOYMENT_PROFILE=hosted \
    NEWSROOM_FRONTEND_SURFACE=portal \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN groupadd --system --gid 10001 agora \
    && useradd --system --uid 10001 --gid agora --no-create-home --shell /usr/sbin/nologin agora
# The server runs from /app/frontend, so it resolves the project root to /app
# exactly as it does in a source checkout.
WORKDIR /app/frontend
COPY --from=build /src/frontend/.next/standalone ./
COPY --from=build /src/frontend/.next/static ./.next/static
COPY --from=build /src/frontend/public ./public
COPY --from=build /src/frontend/data/papers ./data/papers
USER agora
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/api/health').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
CMD ["node", "server.js"]
