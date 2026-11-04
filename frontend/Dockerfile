# Multi-stage build producing a slim Next.js standalone image.
FROM node:20-slim AS deps
WORKDIR /app
COPY package.json ./
RUN npm install

FROM node:20-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:20-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
# Docker sets HOSTNAME to the container id, and Next's standalone server would
# bind only to that address (unreachable on localhost, so health checks fail).
ENV HOSTNAME=0.0.0.0 PORT=3000
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
# The official node image ships an unprivileged "node" user.
USER node
EXPOSE 3000
CMD ["node", "server.js"]
