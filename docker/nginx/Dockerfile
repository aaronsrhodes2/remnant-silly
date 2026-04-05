# Remnant splash + gateway.
#
# Serves:
#   / (splash.html)        — polling UI shown during first-boot model download
#   /status/<service>.json — progress files written by bootstrap downloaders
#                            into the shared remnant-status volume
#   /app/                  — reverse proxy to the sillytavern service once
#                            all services are ready (see nginx.conf)
#
# No network egress needed at runtime. This image ships the splash
# bundle as static assets; the downloaders live in sibling bootstrap
# services (bootstrap-flask-sd, bootstrap-ollama).

FROM nginx:1.27-alpine

# Shared splash bundle — the same files used in native dev testing.
COPY scripts/splash/splash.html   /usr/share/nginx/html/splash.html
COPY scripts/splash/splash.css    /usr/share/nginx/html/splash.css
COPY scripts/splash/splash.js     /usr/share/nginx/html/splash.js

# Diagnostics dashboard — served at /diagnostics/ on the same port.
# Client-side fan-out to /status/*, /health, /api/*, /diagnostics/log.txt.
COPY docker/nginx/diagnostics.html /usr/share/nginx/html/diagnostics.html

# Main nginx config. This is the Docker variant — the proxy upstream
# to sillytavern is uncommented here (nginx.conf on the host is for
# native dev where upstreams don't resolve).
COPY docker/nginx/nginx.conf /etc/nginx/nginx.conf

EXPOSE 80
