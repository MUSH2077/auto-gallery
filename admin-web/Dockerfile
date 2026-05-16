FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json ./
RUN npm config set registry https://registry.npmmirror.com && npm install

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
ARG NEXT_PUBLIC_ADMIN_KEY=changeme
ENV NEXT_PUBLIC_ADMIN_KEY=${NEXT_PUBLIC_ADMIN_KEY}
COPY . .
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
EXPOSE 3000
CMD ["node", "server.js"]
