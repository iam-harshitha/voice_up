# Dashboard image: builds the static bundle, then serves it with nginx, which also
# proxies /api/ to the API on the SAME origin (required for the SameSite=Strict,
# Path=/api/v1 session cookie). TLS is terminated in front of this container.
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
# Linux dependencies are installed here; .dockerignore keeps host node_modules out of
# the context, so the next COPY cannot overwrite them.
RUN npm ci
COPY . .
RUN npm run build && npm run check:bundle

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY nginx-security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
