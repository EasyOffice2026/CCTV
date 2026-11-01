# Web dashboard: the static Next.js export served by nginx, which also proxies /v1 (REST,
# WebSocket and edge long polls) to the API, so the browser talks to one origin.
#   docker build -t cctv-web web/
FROM node:22-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# Empty API URL: same origin, through the proxy below.
RUN NEXT_PUBLIC_API_URL= npm run build

FROM nginx:1.27-alpine
# Substituted at start-up by the nginx image (only these variables; nginx's own $vars stay).
ENV API_UPSTREAM=http://api:8000 \
    MEDIA_ORIGIN=http://localhost:9000 \
    EMBED_ANCESTORS=
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/out /usr/share/nginx/html
EXPOSE 8080
