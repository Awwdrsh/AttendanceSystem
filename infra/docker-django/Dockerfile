# The Django stack in one image; the compose file runs it three times: web, worker and gateway.
# Build context: repository root.
#   docker build -f infra/docker-django/Dockerfile -t hajiri-django .
# infra/docker-django/Dockerfile.dockerignore keeps .env files, virtualenvs, node_modules, data and logs out of the image.
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings
WORKDIR /app

COPY server/requirements.txt server/requirements.txt
# waitress serves the web app and the gateway (pure Python, no compiler needed).
RUN pip install -r server/requirements.txt 'waitress==3.0.2'

COPY server/ server/
COPY scripts/django_ops.py scripts/django_ops.py
COPY infra/docker-django/entrypoint.sh /usr/local/bin/hajiri-entrypoint

# Static files are collected at build time; WhiteNoise serves them from server/staticfiles.
RUN cd server && DATABASE_URL=postgres://build@localhost/build python manage.py collectstatic --noinput >/dev/null \
 && useradd --system --uid 10001 --home-dir /app hajiri \
 && mkdir -p /app/server/data/raw && chown -R hajiri /app/server/data \
 && chmod 755 /usr/local/bin/hajiri-entrypoint
USER hajiri

ENV PORT=3000 RUN_WORKER=false WHITENOISE_USE_FINDERS=false GATEWAY_RAW_DIR=/app/server/data/raw
EXPOSE 3000 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s CMD ["python", "/app/scripts/django_ops.py", "health"]
ENTRYPOINT ["hajiri-entrypoint"]
CMD ["web"]
