# syntax=docker/dockerfile:1
# Laya typed-decision sidecar (Phase 5) as a container: the same pinned package the host
# install uses (laya[serve]==0.3.22), CPU only, non-root. Model files are cached in a volume.

FROM python:3.12-slim-bookworm
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
      "laya[serve]==0.3.22" \
 && groupadd --system --gid 10002 laya \
 && useradd --system --uid 10002 --gid laya --create-home --home-dir /home/laya laya \
 && mkdir -p /models && chown laya:laya /models
ENV LAYA_HOST=0.0.0.0 \
    LAYA_PORT=8765 \
    LAYA_DEVICE=cpu \
    LAYA_MODELS=typed-decisions \
    LAYA_PRELOAD=1 \
    HF_HOME=/models \
    PYTHONUNBUFFERED=1
USER laya
EXPOSE 8765
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=5 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).status == 200 else 1)"]
CMD ["laya-serve"]
