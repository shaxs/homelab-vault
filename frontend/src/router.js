/**
 * Routing — v1 §8.
 *
 * §8 asks for two things: `/catalog` as a real bookmarkable route, and "which
 * page loads at `/`" as a server-side setting. Both are here.
 *
 * **Why this is ~60 lines and not `vue-router`.** The app has five screens, no
 * nested routes, no guards, no lazy chunks and exactly one runtime dependency
 * (`vue`). A router library would be the second, and would be larger than the
 * feature it serves. Reach for one the moment this file grows a second concept.
 *
 * **Modals are deliberately not routed.** Add-service, the write gate, the ⌘K
 * palette and the taxonomy editor stay component state: they are transient, and
 * a URL that restores a half-filled form on reload is worse than one that does
 * not.
 *
 * `/` is resolved by the caller rather than here, because the answer is
 * `ui.landing_page` from the public settings endpoint and this module does no
 * I/O. `resolve()` returns the `home` route and `App.vue` maps it.
 */

import { ref } from 'vue'

/** `/services/{slug}` — the one route with a parameter. */
const RECORD_RE = /^\/services\/([^/]+)\/?$/

const STATIC_ROUTES = {
  '/': { name: 'home' },
  '/catalog': { name: 'catalog' },
  '/dashboard': { name: 'dashboard' },
  '/settings': { name: 'settings' },
}

/**
 * A path this SPA owns.
 *
 * Anything else — a typo, a stale bookmark, a path Django serves — resolves to
 * `notFound` rather than silently rendering the catalog, so a broken link is
 * visible instead of looking like a working link to the wrong page.
 */
export function resolve(pathname) {
  const path = (pathname || '/').replace(/\/+$/, '') || '/'
  if (STATIC_ROUTES[path]) return { ...STATIC_ROUTES[path], path }
  const match = RECORD_RE.exec(path)
  if (match) {
    return { name: 'record', slug: decodeSlug(match[1]), path }
  }
  return { name: 'notFound', path }
}

/**
 * A malformed escape must not throw: `resolve()` runs at module scope on a
 * cold load, so an uncaught URIError from `decodeURIComponent('%zz')` means
 * `main.js` never mounts and the whole app is a blank page — and Django serves
 * `/services/%zz` (its slug pattern matches the invalid sequence literally),
 * so a truncated or mistyped link reaches exactly this line. Falling back to
 * the raw segment lands on the record screen's own "no service with that
 * address" card, which is the graceful path this router exists to provide.
 */
function decodeSlug(segment) {
  try {
    return decodeURIComponent(segment)
  } catch {
    return segment
  }
}

export function pathFor(name, params = {}) {
  if (name === 'record') return `/services/${encodeURIComponent(params.slug)}`
  if (name === 'home') return '/'
  return `/${name}`
}

export const route = ref(resolve(window.location.pathname))

/** Navigate, pushing history so Back works. Same path is a no-op, not a dupe. */
export function navigate(name, params = {}, { replace = false } = {}) {
  const path = pathFor(name, params)
  if (path !== window.location.pathname) {
    window.history[replace ? 'replaceState' : 'pushState']({}, '', path)
  }
  route.value = resolve(path)
  return route.value
}

/** Back/forward must re-render; without this the URL moves and the page does not. */
export function startRouter() {
  window.addEventListener('popstate', onPopState)
}

export function stopRouter() {
  window.removeEventListener('popstate', onPopState)
}

function onPopState() {
  route.value = resolve(window.location.pathname)
}
