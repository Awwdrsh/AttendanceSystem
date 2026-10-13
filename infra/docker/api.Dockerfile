# Build context: repository root.  docker build -f infra/docker/api.Dockerfile -t hajiri-api .
FROM node:22-alpine AS base
RUN corepack enable 2>/dev/null || npm i -g pnpm@12
WORKDIR /app
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml tsconfig.base.json ./
COPY packages ./packages
COPY apps/api ./apps/api
COPY apps/web ./apps/web
RUN pnpm install --frozen-lockfile
RUN pnpm --filter @hajiri/web build
ENV NODE_ENV=production PORT=3000 WEB_DIST=/app/apps/web/dist
EXPOSE 3000
WORKDIR /app/apps/api
CMD ["node", "--import", "tsx", "src/server.ts"]
