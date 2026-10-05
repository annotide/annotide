# syntax=docker/dockerfile:1
#
# frontend/Dockerfile — multi-stage build.
# Stage 1 (build): install deps with npm ci, build the static Vite bundle.
# Stage 2 (runtime): serve dist/ with nginx as a non-root user.

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# Dependency layer cached separately from source changes.
COPY package.json package-lock.json* ./
RUN npm ci

COPY . .

# VITE_API_BASE_URL is baked into the static bundle at build time (Vite env
# vars are compile-time). Compose passes it through as a build arg so the
# frontend service can point at the backend without editing this file.
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

RUN npm run build

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM nginx:alpine AS runtime

# Drop the stock config and use ours (SPA fallback + proxy + security headers).
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY security-headers.conf /etc/nginx/security-headers.conf

# Origins the browser may load signed media from, baked into the CSP. The
# default covers any https object store, which is what a real deployment
# uses. The compose stack passes Azurite's plain-http origin as well; a
# cross-origin http image is otherwise blocked before it is ever requested.
ARG CSP_MEDIA_ORIGINS="https:"
RUN sed -i "s|__CSP_MEDIA_ORIGINS__|${CSP_MEDIA_ORIGINS}|g" /etc/nginx/security-headers.conf; \
    if grep -q "__CSP_MEDIA_ORIGINS__" /etc/nginx/security-headers.conf; then \
        echo "CSP placeholder left unsubstituted"; exit 1; \
    fi

COPY --from=build /app/dist /usr/share/nginx/html

# nginx:alpine already ships an unprivileged "nginx" user; adjust ownership
# and the pidfile location so the master process can run without root.
# The pidfile moves to /tmp (matched by directive, not by path: the image has
# used both /run/nginx.pid and /var/run/nginx.pid across versions, and a sed
# that silently fails to match leaves nginx unable to start as non-root), the `user` directive is dropped (it is ignored
# without root and only emits a warning), and the cache directories are created
# and handed over: nginx writes client_temp, proxy_temp and friends under
# /var/cache/nginx on the first request, and cannot create them itself as a
# non-root user.
RUN sed -i 's#^pid .*#pid /tmp/nginx.pid;#' /etc/nginx/nginx.conf && \
    sed -i '/^user  *nginx;/d' /etc/nginx/nginx.conf && \
    touch /tmp/nginx.pid && \
    mkdir -p /var/cache/nginx/client_temp /var/cache/nginx/proxy_temp \
             /var/cache/nginx/fastcgi_temp /var/cache/nginx/uwsgi_temp \
             /var/cache/nginx/scgi_temp && \
    chown -R nginx:nginx /usr/share/nginx/html /etc/nginx /var/cache/nginx /tmp/nginx.pid && \
    chmod -R g+rwX /usr/share/nginx/html /etc/nginx /var/cache/nginx /tmp/nginx.pid

USER nginx

EXPOSE 80

# 127.0.0.1, not localhost: nginx listens on IPv4 only and busybox wget tries
# ::1 first without falling back, so `localhost` is always refused here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:80/ >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]
