# Frontend build -----------------------------------------------------------
# Base images are pinned by digest so a rebuild months from now uses the image
# that was tested, not whatever the mutable tag points at that day (audit S14).
# To update: bump the tag, `docker pull` it, and copy the new RepoDigest here.
FROM node:22-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS frontend
WORKDIR /build
# No glob on the lockfile: if it is missing this COPY must fail the build, and
# `npm ci` fails on any drift between package.json and the lock instead of
# quietly rewriting the lock inside the image the way `npm install` would.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Runtime -------------------------------------------------------------------
# Pinned to 3.13 rather than tracking the newest release: psycopg ships known
# good wheels here, and the container is the only place a Postgres driver is
# actually used.
FROM python:3.13-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# The hash-pinned lock (uv pip compile --generate-hashes, resolved for Linux —
# regeneration command in its header) is what the image installs. gunicorn is
# in it like everything else; requirements.txt is the human-edited source the
# lock is compiled from. --require-hashes makes pip refuse anything the lock
# does not pin, so a tampered or re-resolved dependency fails the build.
COPY backend/requirements.lock ./backend/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r backend/requirements.lock

COPY backend/ ./backend/
COPY --from=frontend /build/dist ./frontend/dist

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data/media /export \
 && chown -R app:app /app /data /export
USER app

WORKDIR /app/backend
EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3"]
