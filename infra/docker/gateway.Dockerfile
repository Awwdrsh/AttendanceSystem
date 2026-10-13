# docker build -f infra/docker/gateway.Dockerfile -t hajiri-gateway .
FROM node:22-alpine
RUN corepack enable 2>/dev/null || npm i -g pnpm@12
WORKDIR /app
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml tsconfig.base.json ./
COPY packages ./packages
COPY apps/gateway ./apps/gateway
RUN pnpm install --frozen-lockfile --filter @hajiri/gateway...
ENV NODE_ENV=production PORT=8080
EXPOSE 8080
WORKDIR /app/apps/gateway
CMD ["node", "--import", "tsx", "src/server.ts"]
