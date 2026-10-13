#   docker build -f infra/docker/gateway.Dockerfile -t hajiri-gateway .
FROM node:22-alpine
RUN npm i -g pnpm@12
WORKDIR /app
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml tsconfig.base.json ./
COPY packages ./packages
COPY apps ./apps
RUN pnpm install --frozen-lockfile && rm -rf apps/api apps/web
ENV NODE_ENV=production PORT=8080 GATEWAY_RAW_DIR=/app/apps/gateway/data/raw
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s CMD wget -qO- http://localhost:8080/health || exit 1
WORKDIR /app/apps/gateway
CMD ["node", "--import", "tsx", "src/server.ts"]
