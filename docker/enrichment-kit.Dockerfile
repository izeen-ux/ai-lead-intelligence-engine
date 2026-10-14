# syntax=docker/dockerfile:1
# Enrichment sidecar (Phase 6) as a container. Build context: sidecars/enrichment-kit
# setup.mjs clones enrichment-kit at its PINNED commit and builds the library (same as on a host).
# Hunter/Apollo keys are read from the environment at run time - never baked into the image.

FROM node:22-bookworm-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/sidecars/enrichment-kit
COPY package.json setup.mjs lib.mjs server.mjs ./
RUN node setup.mjs

FROM node:22-bookworm-slim AS runtime
WORKDIR /srv/sidecars/enrichment-kit
COPY --from=build /srv/sidecars/enrichment-kit ./
# Inside the private Compose network the server must listen on the container interface.
# server.mjs refuses a non-loopback bind unless ENRICHMENT_KIT_API_TOKEN is set.
ENV ENRICHMENT_KIT_BIND_HOST=0.0.0.0 \
    NODE_ENV=production
USER node
EXPOSE 8787
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:8787/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "server.mjs"]
