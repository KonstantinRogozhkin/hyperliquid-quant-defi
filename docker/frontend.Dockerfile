# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS builder

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

COPY frontend ./

ARG BUILD_SHA=dev
ARG BUILT_AT=""
ENV VITE_BUILD_SHA=$BUILD_SHA \
    VITE_BUILT_AT=$BUILT_AT

RUN npm run build \
    && node -e 'const fs=require("fs"); fs.writeFileSync("/frontend/dist/build-info.json", JSON.stringify({service:"hl-frontend", sha:process.env.VITE_BUILD_SHA||"dev", built_at:process.env.VITE_BUILT_AT||""}, null, 2)+"\n");'

FROM nginx:1.27-alpine AS frontend

ARG BUILD_SHA=dev
ARG BUILT_AT=""

LABEL org.opencontainers.image.revision=$BUILD_SHA \
      org.opencontainers.image.created=$BUILT_AT \
      org.opencontainers.image.title="hyperliquid-quant-defi-frontend"

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /frontend/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/nginx-health >/dev/null 2>&1 || exit 1
