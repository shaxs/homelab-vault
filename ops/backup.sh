#!/usr/bin/env bash
# Nightly database backup — spec §9.
#
#   dump -> verify the file exists, is non-empty and gzip-intact
#        -> retain multiple generations
#        -> restore: gunzip -c <file> | docker compose exec -T db psql ...
#        -> perform one real restore test before trusting this
#
# The dump runs INSIDE the db container via `docker compose exec`, so it needs
# no host port, no pg_dump on the host, and no password handling: the container
# accepts local connections from itself. The cost is that this script must run
# where `docker compose` can find the project — it changes to the repo root
# (one directory above this script) before doing anything.
#
# The output is a gzipped PLAIN-SQL dump. Restore it with psql, never
# pg_restore (pg_restore reads only pg_dump's custom/tar formats):
#
#   gunzip -c backups/homelabvault-<timestamp>.sql.gz \
#     | docker compose exec -T db psql -U homelabvault -d homelabvault
#
# Point BACKUP_DIR somewhere that survives this host being down (a mounted
# share) once you have one; the default keeps generations locally under the
# repo so the script works on any install with no setup.
#
# Cron example (02:15 daily):
#   15 2 * * * BACKUP_DIR=/mnt/backups/homelab-vault /opt/homelab-vault/ops/backup.sh >> /var/log/homelab-vault-backup.log 2>&1

set -Eeuo pipefail

cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETAIN="${RETAIN:-14}"
DB_NAME="${POSTGRES_DB:-homelabvault}"
DB_USER="${POSTGRES_USER:-homelabvault}"
# Minimum plausible dump size in bytes; a schema-only dump is already larger.
MIN_BYTES="${MIN_BYTES:-1024}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="${BACKUP_DIR}/homelabvault-${timestamp}.sql.gz"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { log "ERROR: $*"; exit 1; }

mkdir -p "${BACKUP_DIR}" || fail "cannot create ${BACKUP_DIR}"

log "dumping ${DB_NAME} -> ${target}"
# Write to a temp name first so an interrupted run cannot leave a truncated file
# that looks like a valid generation.
tmp="${target}.partial"
if ! docker compose exec -T db pg_dump --username="${DB_USER}" \
        --no-owner --no-privileges "${DB_NAME}" | gzip -9 > "${tmp}"; then
    rm -f "${tmp}"
    fail "pg_dump failed (is the db container running?)"
fi

# Verify: exists and is non-empty. A zero-byte file is the classic silent
# backup failure — it looks like success to anything that only checks exit codes.
[[ -s "${tmp}" ]] || { rm -f "${tmp}"; fail "dump is empty"; }
size="$(wc -c < "${tmp}")"
(( size >= MIN_BYTES )) || { rm -f "${tmp}"; fail "dump is implausibly small (${size} bytes)"; }
gzip -t "${tmp}" || { rm -f "${tmp}"; fail "dump failed gzip integrity check"; }

mv "${tmp}" "${target}"
log "ok: ${target} (${size} bytes)"

# Retain generations, newest first.
mapfile -t old < <(ls -1t "${BACKUP_DIR}"/homelabvault-*.sql.gz 2>/dev/null | tail -n "+$((RETAIN + 1))")
for f in "${old[@]:-}"; do
    [[ -n "${f}" ]] || continue
    log "pruning ${f}"
    rm -f "${f}"
done

log "done; $(ls -1 "${BACKUP_DIR}"/homelabvault-*.sql.gz 2>/dev/null | wc -l) generation(s) retained"
