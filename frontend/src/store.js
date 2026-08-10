/**
 * Catalog state. A single shared reactive store rather than Pinia — v0 has one
 * collection and one user.
 */

import { computed, reactive, ref } from 'vue'

import { ApiError, api } from './api.js'
import { safeUrl } from './format.js'
import { applyDerivedStatus } from './status.js'

/** How often the open catalog re-fetches health. The worker's default check
 *  interval is 60s, so polling much faster than this only adds traffic. */
const REFRESH_MS = 15000
/** Drives relative timestamps ("checked 22s ago") and stale re-derivation. */
const TICK_MS = 5000

export const state = reactive({
  services: [],
  views: [],
  loading: true,
  error: '',
  /** A background refresh failed; the catalog on screen is the last good one. */
  offline: false,
  query: '',
  selected: new Set(),
  /** True while the write gate is showing (§4c: a gate, not a door). */
  gateOpen: false,
  /** 'sign-in' | 'reauth' — same modal, different reason (§6b). */
  gateReason: 'sign-in',
  authed: false,
  /**
   * Auth mode (§6c), read from the anonymous settings endpoint so the badge
   * renders for a visitor who has never authenticated. Defaults to the safest
   * value: if the request fails we must not imply writes are open.
   */
  authMode: 'full',
  /** Whitelisted display preferences (§6a) — landing page, panel visibility. */
  publicSettings: {},
  /**
   * Per-worker liveness from `/api/v1/healthz`, for the sidebar footer.
   *
   * Read from the heartbeat rather than inferred. The footer used to say
   * "Health worker reporting" based on the count of stale services, which is a
   * decent proxy for that one worker and **no** proxy at all for the Proxmox
   * worker — it has no services to make stale, so it could die unnoticed.
   */
  workers: [],
})

/**
 * The write parked by the gate, plus the promise handles of the caller that is
 * still awaiting it. Holding the resolve/reject is what lets the ORIGINAL caller
 * receive the result once the token is exchanged — without it the retried
 * action's return value is discarded and the UI silently does nothing.
 */
let parked = null

export const isEmptyInstall = computed(
  () => !state.loading && state.services.length === 0,
)

/** Wall clock, ticked centrally so every relative timestamp moves together. */
export const now = ref(Date.now())

/**
 * Display preferences and the current auth mode, for an anonymous viewer.
 *
 * Never throws: a failure here must not stop the catalog rendering, and the
 * defaults are the safe ones — `full` claims writes are gated, which is the
 * error worth making if we cannot tell.
 */
export async function loadPublicSettings() {
  try {
    const { auth_mode: authMode, values } = await api.getPublicSettings()
    state.authMode = authMode || 'full'
    state.publicSettings = values || {}
  } catch {
    state.authMode = 'full'
  }
}

/**
 * Worker liveness. Never throws, and **treats 503 as data**.
 *
 * `/healthz` answers 503 precisely when a worker is stalled (§5a) — which is
 * exactly the state the footer exists to show. Letting `ApiError` propagate
 * would blank the indicator at the one moment it matters, so the body is read
 * off the error and used.
 */
export async function loadWorkers() {
  try {
    const body = await api.getHealthz()
    state.workers = body?.workers || []
  } catch (err) {
    const body = err instanceof ApiError ? err.body : null
    // A stalled worker is a 503 with a perfectly good body.
    state.workers = body?.workers || []
  }
}

/**
 * Which catalog write is the newest. A slow in-flight poll can resolve AFTER a
 * save has already applied fresher data locally — or after a newer poll, since
 * the `visibilitychange` refresh overlaps the interval — and writing its
 * payload would visibly revert a rename for up to one interval (audit
 * informational). Bumped by every load and every local write; only a load
 * still holding the current number may touch state.
 */
let catalogGeneration = 0

export async function loadCatalog({ quiet = false } = {}) {
  const generation = ++catalogGeneration
  if (!quiet) state.loading = true
  try {
    const [services, views] = await Promise.all([
      api.listServices(),
      api.listViews(),
    ])
    // Superseded while in flight: this payload is older than what is on
    // screen. Drop it; the next poll carries the truth.
    if (generation !== catalogGeneration) return
    state.services = applyDerivedStatus(services, Date.now())
    state.views = views
    state.error = ''
    state.offline = false
  } catch (err) {
    if (generation !== catalogGeneration) return
    // A failed background poll must not blank the catalog or throw a banner
    // over a page that is still perfectly readable.
    if (quiet) state.offline = true
    else state.error = err.message || 'Could not load the catalog.'
  } finally {
    if (generation === catalogGeneration) state.loading = false
  }
}

let refreshTimer = null
let tickTimer = null

function tick() {
  now.value = Date.now()
  // Re-derive locally so a stopped worker surfaces as `stale` even between
  // polls, or if polling itself has failed.
  state.services = applyDerivedStatus(state.services, now.value)
}

function onVisibilityChange() {
  if (document.visibilityState === 'visible') {
    // Coming back to a tab that has been hidden for a while: refresh at once
    // rather than showing stale data until the next interval elapses.
    loadCatalog({ quiet: true })
    loadWorkers()
  }
}

export function startAutoRefresh() {
  stopAutoRefresh()
  refreshTimer = setInterval(() => {
    // Polling a hidden tab is pure waste; visibilitychange catches it up.
    if (document.visibilityState === 'visible') {
      loadCatalog({ quiet: true })
      loadWorkers()
    }
  }, REFRESH_MS)
  tickTimer = setInterval(tick, TICK_MS)
  document.addEventListener('visibilitychange', onVisibilityChange)
}

export function stopAutoRefresh() {
  clearInterval(refreshTimer)
  clearInterval(tickTimer)
  refreshTimer = null
  tickTimer = null
  document.removeEventListener('visibilitychange', onVisibilityChange)
}

/**
 * Run a write, and if it comes back unauthorised, park it for the write gate.
 *
 * "UI authentication is a write gate, not a door" (§4c): the catalog renders
 * fully for an anonymous visitor and the token is requested only when a write
 * is actually attempted — then the original action proceeds.
 */
export async function withWriteGate(action) {
  try {
    const result = await action()
    state.authed = true
    return result
  } catch (err) {
    if (!(err instanceof ApiError) || !err.needsAuth) throw err

    // Opening the gate moves focus, which can fire a blur handler that starts a
    // second gated write. Release the previous caller rather than overwriting
    // it, or its promise never settles and that component waits forever.
    if (parked) {
      parked.resolve(undefined)
      parked = null
    }

    // Park the action AND the caller. The returned promise stays unresolved
    // until the token is exchanged, so `await withWriteGate(...)` resumes with
    // the real result rather than the caller having already given up on
    // `undefined`.
    state.gateReason = err.needsReauth ? 'reauth' : 'sign-in'
    state.gateOpen = true
    return new Promise((resolve, reject) => {
      parked = { action, resolve, reject }
    })
  }
}

/** Called once the admin token has been exchanged for a session cookie. */
export async function resumeAfterUnlock() {
  state.gateOpen = false
  state.gateReason = 'sign-in'
  state.authed = true
  const pending = parked
  parked = null
  if (!pending) return

  try {
    // Deliberately NOT routed back through withWriteGate: a second 401 here
    // means the scope is genuinely wrong (an agent attempting DELETE, say), and
    // re-opening the gate would loop.
    pending.resolve(await pending.action())
  } catch (err) {
    pending.reject(err)
  }
}

/** Gate dismissed without authenticating. */
export function cancelGate() {
  state.gateOpen = false
  state.gateReason = 'sign-in'
  const pending = parked
  parked = null
  // Resolve rather than reject: callers already treat a falsy result as
  // "refused", and a rejection here would surface as an error the user caused
  // deliberately by pressing Cancel.
  if (pending) pending.resolve(undefined)
}

/**
 * Open a service.
 *
 * DEVIATES from §3's "window.open with named target per service" — ruling
 * 2026-08-03. Named-target reuse was verified not to work in a real browser
 * (twice, including incognito; the target services send no COOP headers, so
 * nothing server-side explains it). Keeping it required omitting `noopener`,
 * because browsers treat any target name other than _top/_self/_parent as
 * `_blank` when `noopener` is present — so the catalog was paying a real
 * reverse-tabnabbing exposure for a feature that never functioned.
 *
 * Every launch is now `_blank` with `noopener`, so a launched service can never
 * reach back through `window.opener` to navigate the catalog tab.
 *
 * `newTab` still matters: it overrides a service's own `same_tab` preference.
 */
export function launch(service, newTab = false) {
  // `launch_url` is stored as free text with no scheme validation on the write
  // path, and `location.href = 'javascript:…'` executes. Guarded here as well
  // as at the drawer's `href` because this is the other place a service-supplied
  // URL becomes a navigation (api-security gate, V1.5).
  const url = safeUrl(service.launch_url)
  if (!url) return
  if (service.launch_target === 'same_tab' && !newTab) {
    window.location.href = url
    return
  }
  window.open(url, '_blank', 'noopener')
}

export function upsertLocal(service) {
  // This write is newer than any poll already in the air — a response that
  // left the server before this save must not land on top of it.
  catalogGeneration += 1
  const [derived] = applyDerivedStatus([service], Date.now())
  const index = state.services.findIndex((s) => s.slug === derived.slug)
  if (index === -1) state.services.push(derived)
  else state.services[index] = derived
}

/** Run a service's health check now rather than waiting for its interval.
 *
 *  The response only carries the health fields, so it is merged onto the
 *  existing record rather than replacing it — otherwise the row would lose its
 *  name, tags and icon until the next poll.
 */
export async function recheckHealth(slug) {
  const health = await withWriteGate(() => api.recheck(slug))
  if (!health) return undefined
  const index = state.services.findIndex((s) => s.slug === slug)
  if (index !== -1) {
    const merged = { ...state.services[index], ...health }
    state.services[index] = applyDerivedStatus([merged], Date.now())[0]
  }
  return health
}

export function removeLocal(slug) {
  // Same rule as upsertLocal: an in-flight poll predates this delete and
  // would resurrect the row.
  catalogGeneration += 1
  state.services = state.services.filter((s) => s.slug !== slug)
}

/** Free-text values exist because a service uses one and vanish when none do (§6b). */
export const allTags = computed(() => {
  const seen = new Set()
  for (const service of state.services) {
    for (const tag of service.tags || []) seen.add(tag)
  }
  return [...seen].sort()
})

export const allCategories = computed(() => {
  const counts = new Map()
  for (const service of state.services) {
    if (!service.category) continue
    counts.set(service.category, (counts.get(service.category) || 0) + 1)
  }
  return [...counts.entries()]
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => a.name.localeCompare(b.name))
})
