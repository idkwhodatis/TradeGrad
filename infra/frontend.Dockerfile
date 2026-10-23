ARG NODE_IMAGE=node:24-bookworm-slim
ARG NGINX_IMAGE=nginx:stable-alpine
FROM ${NODE_IMAGE} AS build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM ${NGINX_IMAGE}
COPY infra/nginx.conf /etc/nginx/nginx.conf
COPY --from=build /build/dist /usr/share/nginx/html
COPY frontend/FASTAPI-TEMPLATE-LICENSE frontend/LIGHTWEIGHT-CHARTS-NOTICE frontend/THIRD_PARTY_NOTICES.md /usr/share/nginx/html/notices/
USER 65532:65532
ENTRYPOINT ["nginx", "-g", "daemon off;"]
