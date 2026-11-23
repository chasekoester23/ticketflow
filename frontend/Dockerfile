# TicketFlow frontend: the Next.js standalone server, which also proxies /api/... to the
# backend at API_INTERNAL_URL, so the browser only ever talks to this one origin.
# Build from frontend/:  docker build -t ticketflow-frontend frontend
# See docs/deploy/railway.md.

FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22-slim AS build
WORKDIR /app
# Empty means same-origin (/api/... on this site). Inlined into the browser code at
# build time, so it must be set here, not at run time.
ARG NEXT_PUBLIC_API_URL=""
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_OUTPUT=standalone \
    NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build \
 && if [ -z "$NEXT_PUBLIC_API_URL" ] && grep -rq "localhost:8000" .next/static; then \
      echo "The browser bundle still points at localhost:8000" >&2; exit 1; fi

FROM node:22-slim
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
WORKDIR /app
# Owned by root and read-only to the server, except Next's own cache folder.
COPY --from=build /app/public ./public
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
RUN mkdir -p .next/cache && chown node:node .next/cache
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:' + (process.env.PORT || 3000) + '/login').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
CMD ["node", "server.js"]
