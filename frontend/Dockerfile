# syntax=docker/dockerfile:1
# Consola web de AegisVolt. Contexto de build: la RAÍZ del repo.
FROM node:24-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.31-alpine
ARG VERSION=dev-local
LABEL org.opencontainers.image.title="aegisvolt-frontend" org.opencontainers.image.version="${VERSION}"
USER root
RUN apk upgrade --no-cache
COPY frontend/nginx.conf /etc/nginx/nginx.conf
COPY frontend/security-headers.conf /etc/nginx/security-headers.conf
COPY --from=build /app/dist /usr/share/nginx/html
USER 101
EXPOSE 8080
# Sin el entrypoint de la imagen: sus scripts intentan escribir la configuración (FS read-only).
ENTRYPOINT ["nginx", "-g", "daemon off;"]
