# Build context: repository root (keep the root .dockerignore so .env, node_modules, data and logs never enter the image).
#   docker build -f infra/docker/api.Dockerfile -t hajiri-api .
FROM node:22-alpine
RUN npm i -g pnpm@12
WORKDIR /app
# The whole workspace is needed for a frozen install (the lockfile lists every workspace package).
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml tsconfig.base.json ./
COPY packages ./packages
COPY apps ./apps
COPY infra/docker/api-entrypoint.sh /app/api-entrypoint.sh
RUN pnpm install --frozen-lockfile && pnpm --filter @hajiri/web build && rm -rf apps/gateway
ENV NODE_ENV=production PORT=3000 WEB_DIST=/app/apps/web/dist
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s CMD wget -qO- http://localhost:3000/health || exit 1
ENTRYPOINT ["/bin/sh", "/app/api-entrypoint.sh"]
