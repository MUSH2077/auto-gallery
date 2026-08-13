FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS deps
WORKDIR /app
COPY admin-web/package.json admin-web/package-lock.json ./
ARG NPM_REGISTRY
RUN if [ -n "$NPM_REGISTRY" ]; then \
      npm config set registry "$NPM_REGISTRY"; \
    fi && npm ci

FROM deps AS builder
ARG CACHEBUST=1
COPY admin-web/ .
# The generated client contract is checked against the repository's canonical
# OpenAPI document. The root build context is intentional and remains small
# through the repository .dockerignore.
COPY docs/api/openapi.json /docs/api/openapi.json
ARG ADMIN_BUILD_NODE_HEAP_MB=640
ARG NEXT_PUBLIC_WS_URL
ARG NEXT_PUBLIC_SOURCE_CODE_URL=https://github.com/MUSH2077/auto-gallery
ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_SKIP_INTERNAL_TYPECHECK=1
ENV NODE_OPTIONS="--max-old-space-size=${ADMIN_BUILD_NODE_HEAP_MB}"
ENV NEXT_PUBLIC_WS_URL=$NEXT_PUBLIC_WS_URL
ENV NEXT_PUBLIC_SOURCE_CODE_URL=$NEXT_PUBLIC_SOURCE_CODE_URL
# Next compilation and TypeScript checking deliberately run in different
# processes. This avoids adding both peaks inside the bounded BuildKit cgroup.
RUN npm run build

FROM builder AS checks
RUN npm run check:i18n && \
    npm run check:charts && \
    npm run check:media && \
    npm run check:search-contract && \
    npm run check:admin-routes && \
    npm run check:api-types && \
    npm run typecheck && \
    touch /tmp/admin-web-checks.pass

FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
# This marker is intentionally copied from the checks stage. It makes the
# complete contract/typecheck stage a required dependency of the final image.
COPY --from=checks /tmp/admin-web-checks.pass /tmp/admin-web-checks.pass
COPY admin-web/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 3000
ENTRYPOINT ["/entrypoint.sh"]
