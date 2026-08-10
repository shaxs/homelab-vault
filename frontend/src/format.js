/**
 * Display formatting for the dashboard — v1 §7d.
 *
 * The panel endpoint ships raw numbers with explicit units (bytes, seconds,
 * percentages) and formatting happens here, once. Two reasons: a v2 widget
 * consuming the same pipe wants the numbers, and a percentage computed in two
 * places is a percentage that will eventually disagree with itself.
 *
 * Binary units throughout (TiB, GiB), because that is what Proxmox reports and
 * what its own UI shows — a dashboard that says 3.91 TB beside a PVE console
 * saying 3.91 TiB invites an operator to go looking for the missing 10%.
 */

const UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']

/** `1003592089600` -> `934 GiB`. Null and undefined render as an em dash. */
export function bytes(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'
  let size = Number(value)
  let unit = 0
  while (size >= 1024 && unit < UNITS.length - 1) {
    size /= 1024
    unit += 1
  }
  // Whole bytes never want decimals, and neither does a round KiB.
  const places = unit === 0 ? 0 : digits
  return `${size.toFixed(places)} ${UNITS[unit]}`
}

/** `2.21 / 3.91 TiB` — one unit, stated once, so the pair is comparable. */
export function bytesPair(used, total) {
  if (!total) return '—'
  let unit = 0
  let scale = 1
  while (total / scale >= 1024 && unit < UNITS.length - 1) {
    scale *= 1024
    unit += 1
  }
  return `${(used / scale).toFixed(2)} / ${(total / scale).toFixed(2)} ${UNITS[unit]}`
}

/** `609946` -> `7d 01:25`. Days matter on a hypervisor; seconds do not. */
export function uptime(seconds) {
  if (seconds === null || seconds === undefined) return '—'
  const total = Math.max(0, Math.floor(Number(seconds)))
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const clock = `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`
  return days ? `${days}d ${clock}` : clock
}

export function percent(value, digits = 1) {
  if (value === null || value === undefined) return '—'
  return `${Number(value).toFixed(digits)}%`
}

/** A width for a bar. Clamped, because a percentage over 100 breaks the layout. */
export function barWidth(value) {
  const pct = Number(value)
  if (!Number.isFinite(pct)) return '0%'
  return `${Math.min(100, Math.max(0, pct))}%`
}

/** `2026-08-07T02:48:39Z` -> `4s ago`. The mockup's "polled 4s ago" stamp. */
export function since(timestamp, now = Date.now()) {
  if (!timestamp) return 'never'
  const then = new Date(timestamp).getTime()
  if (Number.isNaN(then)) return 'never'
  const seconds = Math.max(0, Math.round((now - then) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 48) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

/** Two decimals, or an em dash. A load average of exactly 0 is still a number. */
export function load(value) {
  if (value === null || value === undefined) return '—'
  return Number(value).toFixed(2)
}

/**
 * A service URL that is safe to put in an `href`, or `null`.
 *
 * **Found by V1.5's `api-security` gate.** `launch_url` is a plain text column
 * with no scheme validation on the write path, and under `open-writes` (§6c)
 * anyone on the tailnet can set it. `<a href="javascript:…">` executes on click
 * in every browser, and this app's CSRF token is readable by design, so a
 * stored `javascript:` URL is an admin-action primitive.
 *
 * Only `http` and `https` produce a link. Anything else renders as text, which
 * is also the more useful failure: an operator sees the value that is wrong.
 *
 * The server now refuses non-http(s) schemes at write time too (the 2026-08-10
 * breaking change; migration 0007 repaired pre-rule rows). This guard stays as
 * the sink-side layer of that defense: rows written around the API — manual
 * psql, a migration bug — must still never become clickable.
 */
export function safeUrl(value) {
  const text = (value || '').trim()
  if (!text) return null
  try {
    // `window.location.origin` only matters for relative inputs; the scheme
    // check below is what decides.
    const parsed = new URL(text, window.location.origin)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? text : null
  } catch {
    return null
  }
}
