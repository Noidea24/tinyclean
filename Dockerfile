# mediatool dashboard. Python standard library only: nothing is installed.
FROM python:3.12-alpine
ARG VERSION=dev
LABEL org.opencontainers.image.title="mediatool dashboard" \
      org.opencontainers.image.source="https://github.com/Noidea24/tinyclean" \
      org.opencontainers.image.version="$VERSION"
COPY dashboard /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MEDIATOOL_VERSION=$VERSION
USER nobody
EXPOSE 8780
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8780/healthz', timeout=3)"
CMD ["python", "/app/server.py"]
