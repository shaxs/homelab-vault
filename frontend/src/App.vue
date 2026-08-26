<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

import { api } from './api.js'
import AddService from './components/AddService.vue'
import BaseModal from './components/BaseModal.vue'
import CatalogTable from './components/CatalogTable.vue'
import ComboInput from './components/ComboInput.vue'
import CommandPalette from './components/CommandPalette.vue'
import Dashboard from './components/Dashboard.vue'
import EmptyState from './components/EmptyState.vue'
import ServiceRecord from './components/ServiceRecord.vue'
import SettingsScreen from './components/SettingsScreen.vue'
import TagInput from './components/TagInput.vue'
import TaxonomyEdit from './components/TaxonomyEdit.vue'
import WriteGate from './components/WriteGate.vue'
import { navigate, route, startRouter, stopRouter } from './router.js'
import {
  allCategories,
  allTags,
  cancelGate,
  isEmptyInstall,
  loadCatalog,
  loadPublicSettings,
  loadWorkers,
  recheckHealth,
  resumeAfterUnlock,
  startAutoRefresh,
  state,
  stopAutoRefresh,
  upsertLocal,
  withWriteGate,
} from './store.js'
import {
  SYNTAX_HELP,
  filterServices,
  hasTerm,
  selectTerm,
  toggleTerm,
  tokenize,
} from './search.js'
import { useTheme } from './theme.js'
import { useMediaQuery } from './viewport.js'

const theme = useTheme()

/**
 * Which screen is showing — now derived from the URL rather than held in a ref
 * (§8). `/catalog` is bookmarkable, and so are the settings screen and a
 * service record: before this, reloading either dropped you back on the catalog
 * with no way to link a colleague to a service (`BACKLOG.3`).
 *
 * `home` is the transient state before `ui.landing_page` has been read; the
 * mount handler replaces it with the real route, so it never stays on screen.
 */
const screen = computed(() => route.value.name)
const paletteOpen = ref(false)
const addOpen = ref(false)
const syntaxOpen = ref(false)
const savingView = ref(false)
const newViewName = ref('')
/** Failure inside the save-view modal — shown there, like AddService does. */
const viewError = ref('')
const bulkTagOpen = ref(false)
const bulkTagValue = ref([])
const bulkCatOpen = ref(false)
const bulkCatValue = ref('')
const bulkBusy = ref(false)
/** Failure inside either bulk modal; only one of the two is ever open. */
const bulkError = ref('')

// A failure message belongs to one showing of a modal: opening or closing any
// of the three starts fresh rather than replaying the last attempt's error.
watch([savingView, bulkTagOpen, bulkCatOpen], () => {
  viewError.value = ''
  bulkError.value = ''
})
const banner = ref('')
/** { kind: 'category' | 'tag', name } while the rename dialog is open. */
const editingTaxonomy = ref(null)

/**
 * Below the desktop breakpoint the sidebar is an overlay drawer, opened by the
 * topbar's ☰. The markup is the same <aside> desktop shows — one nav, two
 * presentations — so saved views and categories need no phone-only surface.
 */
const navOpen = ref(false)
/**
 * Any navigation dismisses the drawer — it exists to get you somewhere. This
 * single watcher covers same-screen taps too, because `navigate()` reassigns
 * `route.value` with a fresh object even when the path is unchanged
 * (router.js:82) — App.test.js pins that: if navigate() ever grows a same-path
 * early return, the "already on" drawer test goes red and points here.
 */
watch(route, () => {
  navOpen.value = false
})
/**
 * A drawer opened on a narrow window must not linger as stale state: without
 * this, widening past the breakpoint and narrowing again would pop the drawer
 * back open unasked. jsdom reports no match, so tests run desktop-mode.
 */
const belowDesktop = useMediaQuery('(max-width: 1099.98px)')
watch(belowDesktop, (below) => {
  if (!below) navOpen.value = false
})
/**
 * Any modal opening closes the drawer. This is what keeps Escape unambiguous:
 * BaseModal owns Escape through its module-level stack, and the App-level
 * handler below only ever sees Escape when no modal is up — so the two never
 * race for the same keypress.
 */
watch([savingView, editingTaxonomy, paletteOpen, addOpen, bulkTagOpen, bulkCatOpen], (flags) => {
  if (flags.some(Boolean)) navOpen.value = false
})

async function onTaxonomyChanged(message) {
  showBanner(message)
  // The change touched many services at once; reload rather than patching
  // each one locally.
  await loadCatalog({ quiet: true })
}

const rows = computed(() => filterServices(state.services, state.query))

const counts = computed(() => {
  const tally = { up: 0, down: 0, stale: 0, unknown: 0, off: 0 }
  for (const service of state.services) {
    if (tally[service.status] !== undefined) tally[service.status] += 1
  }
  return tally
})

/** The header counts double as status filters — the numbers are already there,
 *  so making them clickable beats adding a second row of controls. */
const STATUS_FILTERS = [
  { key: 'up', label: 'up', color: 'var(--ok)' },
  { key: 'down', label: 'down', color: 'var(--err)' },
  { key: 'stale', label: 'stale', color: 'var(--warn)' },
  { key: 'unknown', label: 'unknown', color: 'var(--txt3)' },
  { key: 'off', label: 'no check', color: 'var(--txt3)' },
]

function toggleStatus(key) {
  state.query = toggleTerm(state.query, `state:${key}`)
  navigate('catalog')
}

function clearQuery() {
  state.query = ''
}

/** Which screen `/` resolves to, from `ui.landing_page` (§8). */
const landingPage = computed(() =>
  state.publicSettings['ui.landing_page'] === 'dashboard' ? 'dashboard' : 'catalog',
)
const landingLabel = computed(() =>
  landingPage.value === 'dashboard' ? 'Command Center' : 'catalog',
)

/** The catalog, unfiltered and with nothing selected. */
function goCatalog() {
  state.query = ''
  state.selected = new Set()
  syntaxOpen.value = false
  navigate('catalog')
}

/**
 * Home: the operator's chosen landing page, not a hardcoded catalog.
 *
 * Someone who set the Command Center as their landing page meant it, and the
 * brand is the one control on screen that says "take me back to the start".
 * The catalog state is still cleared either way, so returning here and then
 * clicking Catalog gives an unfiltered list rather than yesterday's query.
 */
function goHome() {
  state.query = ''
  state.selected = new Set()
  syntaxOpen.value = false
  navigate(landingPage.value)
}

const WORKER_LABELS = {
  health: 'Health worker',
  proxmox: 'Proxmox worker',
  // TN2: a new worker needs its label here, or it renders as the raw key
  // ("truenas reporting") — the fallback below is for genuinely-unknown workers.
  // TrueNAS is not title-case ("Truenas"), so it must be spelled out.
  truenas: 'TrueNAS worker',
}

/** Worker liveness for the sidebar foot, from the heartbeat (§5a). */
const workerRows = computed(() =>
  state.workers.map((worker) => ({
    name: worker.name,
    label: WORKER_LABELS[worker.name] || worker.name,
    state: worker.stale ? 'not reporting' : 'reporting',
    colour: worker.stale ? 'var(--err)' : 'var(--ok)',
  })),
)

/** The service a `/services/{slug}` URL names, once the catalog has loaded. */
const current = computed(() => {
  if (route.value.name !== 'record') return null
  return state.services.find((s) => s.slug === route.value.slug) || null
})

let bannerTimer = null

/** Confirmation messages should not linger on screen indefinitely. */
function showBanner(message) {
  banner.value = message || 'Saved.'
  clearTimeout(bannerTimer)
  bannerTimer = setTimeout(() => {
    banner.value = ''
  }, 6000)
}

/** Suggest the tags not already in the query (§6b autocomplete). */
const tagSuggestions = computed(() =>
  allTags.value.filter((t) => !hasTerm(state.query, `tag:${t}`)).slice(0, 10),
)

/** Every term currently narrowing the catalog, each individually removable. */
const activeTerms = computed(() => tokenize(state.query))

const activeView = computed(() =>
  state.views.find((v) => v.query === state.query.trim()),
)

function onKey(event) {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    paletteOpen.value = true
  }
  // Safe to take unconditionally: any open modal already closed the drawer
  // (see the watcher above), so this never competes with BaseModal's stack.
  if (event.key === 'Escape' && navOpen.value) {
    navOpen.value = false
  }
}

onMounted(async () => {
  document.addEventListener('keydown', onKey)
  startRouter()
  loadCatalog()
  loadWorkers()
  // The catalog is meant to sit open as a browser home page, so health has to
  // keep moving without a manual refresh.
  startAutoRefresh()
  // The mode badge must render for a visitor who has never authenticated
  // (§6c: "the UI must show the current mode persistently") — and `/` cannot
  // resolve until this lands, because which page loads there is a setting (§8).
  await loadPublicSettings()
  if (route.value.name === 'home') {
    const landing = state.publicSettings['ui.landing_page'] === 'dashboard'
      ? 'dashboard'
      : 'catalog'
    // `replace`, not push: `/` redirecting to `/catalog` must not put a step in
    // the history that Back immediately bounces off.
    navigate(landing, {}, { replace: true })
  }
})
onUnmounted(() => {
  document.removeEventListener('keydown', onKey)
  stopRouter()
  stopAutoRefresh()
  clearTimeout(bannerTimer)
})

function openRecord(service) {
  navigate('record', { slug: service.slug })
}

function applyQuery(query) {
  state.query = query
  navigate('catalog')
}

function addTerm(term) {
  state.query = toggleTerm(state.query, term)
  navigate('catalog')
}

/**
 * Single-select, for a value a service can only have one of.
 *
 * Used by the catalog's Category chips and by the sidebar's category list, so
 * the two agree: clicking one category replaces the last rather than adding to
 * it. Tags keep `addTerm`, because a service really does have many.
 */
function selectOne(term) {
  state.query = selectTerm(state.query, term)
  navigate('catalog')
}

/**
 * Categories are single-select from the sidebar (operator ruling 2026-08-07).
 *
 * They used to accumulate, on the reasoning that repeated `cat:` terms OR
 * together — true, and still true when you *type* them, but as a click
 * affordance it reads as a bug: clicking AI and then Infrastructure showed both
 * and there was no obvious way back. A service has exactly one category, so
 * picking one means picking one.
 *
 * `selectTerm` changes the click only. The query language is untouched, which
 * is what keeps existing saved views matching what they always matched. Tags
 * still use `addTerm` and still accumulate — a service really does have many.
 */
function toggleCategory(name) {
  selectOne(`cat:${name}`)
}

async function saveView() {
  const name = newViewName.value.trim()
  if (!name) return
  viewError.value = ''
  try {
    const created = await withWriteGate(() =>
      api.createView({ name, query: state.query, position: state.views.length }),
    )
    if (!created) return
    state.views = [...state.views.filter((v) => v.name !== name), created]
    savingView.value = false
    newViewName.value = ''
  } catch (err) {
    // `withWriteGate` only intercepts the needs-auth case; a 422, 500 or
    // network failure used to reject to the console, leaving the modal open
    // and the button reading as dead (audit S11).
    viewError.value = err.message || 'Could not save the view.'
  }
}

/** Row multi-select "add tag" — the second §6b mechanism. */
async function applyBulkTag() {
  const tags = bulkTagValue.value
  if (!tags.length) return
  bulkError.value = ''
  const slugs = [...state.selected]
  try {
    for (const slug of slugs) {
      const service = state.services.find((s) => s.slug === slug)
      if (!service) continue
      const merged = [...(service.tags || [])]
      for (const tag of tags) {
        if (!merged.includes(tag)) merged.push(tag)
      }
      if (merged.length === (service.tags || []).length) continue
      const saved = await withWriteGate(() => api.patchService(slug, { tags: merged }))
      if (!saved) return
      upsertLocal(saved)
    }
  } catch (err) {
    // Mid-loop failure: services already patched keep their tags (each write
    // stands alone); the modal stays open so the rest can be retried.
    bulkError.value = err.message || 'Could not tag the selected services.'
    return
  }
  bulkTagOpen.value = false
  bulkTagValue.value = []
  state.selected = new Set()
  showBanner(`Tagged ${slugs.length} service(s).`)
}

async function applyBulkCategory() {
  const category = bulkCatValue.value.trim()
  bulkError.value = ''
  const slugs = [...state.selected]
  try {
    for (const slug of slugs) {
      const saved = await withWriteGate(() =>
        api.patchService(slug, { category: category || null }),
      )
      if (!saved) return
      upsertLocal(saved)
    }
  } catch (err) {
    bulkError.value = err.message || 'Could not set the category.'
    return
  }
  bulkCatOpen.value = false
  bulkCatValue.value = ''
  state.selected = new Set()
  showBanner(
    category
      ? `Set ${slugs.length} service(s) to “${category}”.`
      : `Cleared the category on ${slugs.length} service(s).`,
  )
}

async function recheckSelected() {
  bulkBusy.value = true
  const slugs = [...state.selected]
  try {
    for (const slug of slugs) {
      const service = state.services.find((s) => s.slug === slug)
      if (service?.health_enabled) await recheckHealth(slug)
    }
    showBanner(`Re-checked ${slugs.length} service(s).`)
  } catch (err) {
    state.error = err.message || 'Could not re-check.'
  } finally {
    bulkBusy.value = false
  }
}

async function onGateAuthenticated() {
  // Resolve the parked caller first so its own UI updates, then refresh.
  await resumeAfterUnlock()
  await loadCatalog()
}
</script>

<template>
  <div class="app" :class="{ 'nav-open': navOpen }">
    <!-- Below the desktop breakpoint the backdrop sits between the content and
         the sidebar-as-drawer; tapping it is the touch equivalent of Escape. -->
    <div v-if="navOpen" class="nav-backdrop" @click="navOpen = false" />

    <!-- Sidebar: primary nav, then saved views and categories (§5).
         Same markup at every width — desktop shows it as a fixed rail, narrow
         viewports slide it in as an overlay drawer (styles.css, responsive). -->
    <aside class="sidebar">
      <!-- Home: whichever screen the operator chose as their landing page, not
           a hardcoded catalog. Someone who set the dashboard as home means it. -->
      <button class="brand" :title="`Back to the ${landingLabel}`" @click="goHome">
        <span class="brand-mark">H</span>
        <span class="brand-name">Homelab Vault</span>
      </button>

      <!-- Primary navigation. A surface, a border and an active dot — rather than reusing the plain `.side-item` used by
           saved views and categories below, because sharing that styling was
           what made these two read as more links in the same list. -->
      <nav class="side-nav" aria-label="Primary">
        <button
          class="nav-item"
          :class="{ active: screen === 'dashboard' }"
          @click="navigate('dashboard')"
        >
          <span class="nav-label">Command Center</span>
          <span class="nav-dot" />
        </button>
        <button
          class="nav-item"
          :class="{ active: screen === 'catalog' }"
          @click="goCatalog"
        >
          <span class="nav-label">Catalog</span>
          <span class="nav-dot" />
        </button>
      </nav>


      <template v-if="state.services.length">
        <div class="side-group">
          <div class="side-head">
            <span>Saved views</span>
            <button
              class="link-btn"
              style="margin-left: auto"
              title="Save the current query as a view"
              @click="savingView = true"
            >
              ＋
            </button>
          </div>
          <div
            v-for="view in state.views"
            :key="view.id"
            class="side-item"
            :class="{ active: activeView && activeView.id === view.id }"
            @click="applyQuery(view.query)"
          >
            <span class="side-item-label">{{ view.name }}</span>
            <!-- Renaming lives here for the same reason it does on categories:
                 where the value already appears, not on a settings screen. A
                 typo in a view name was previously unfixable except by deleting
                 it and rebuilding the query. -->
            <button
              class="side-edit"
              :title="`Rename or delete ${view.name}`"
              @click.stop="editingTaxonomy = { kind: 'view', name: view.name }"
            >
              ✎
            </button>
            <span class="side-count">{{ filterServices(state.services, view.query).length }}</span>
          </div>
          <!-- Indented to sit with the rows it stands in for. -->
          <div v-if="!state.views.length" class="side-empty-body" style="padding: 4px 10px 4px 20px">
            Save any query as a view.
          </div>
        </div>

        <div class="side-group">
          <div class="side-head">Categories</div>
          <div
            v-for="cat in allCategories"
            :key="cat.name"
            class="side-item"
            :class="{ active: hasTerm(state.query, `cat:${cat.name}`) }"
            @click="toggleCategory(cat.name)"
          >
            <span class="side-item-label">{{ cat.name }}</span>
            <!-- Renaming lives here, where the value already is: §5 rules out a
                 settings screen, and a typo is otherwise unfixable in bulk. -->
            <button
              class="side-edit"
              title="Rename or remove this category"
              @click.stop="editingTaxonomy = { kind: 'category', name: cat.name }"
            >
              ✎
            </button>
            <span class="side-count">{{ cat.count }}</span>
          </div>
        </div>
      </template>

      <div v-else class="side-empty">
        <div class="side-empty-title">No saved views yet</div>
        <div class="side-empty-body">
          Views and categories appear here once you have added a service.
        </div>
      </div>

      <!-- Foot: worker liveness above the rule, Settings below it. Both workers
           are shown, and both come from the heartbeat rather than being
           inferred from stale service counts — that inference never could have
           covered the Proxmox worker, which has no services to make stale. -->
      <div class="side-foot">
        <div v-if="state.offline" class="worker-status">
          <span class="dot" style="background: var(--err)" />
          <span class="worker-text">Not refreshing — server unreachable</span>
        </div>
        <template v-else>
          <div v-for="worker in workerRows" :key="worker.name" class="worker-status">
            <span class="dot" :style="{ background: worker.colour }" />
            <span class="worker-text">{{ worker.label }} {{ worker.state }}</span>
          </div>
          <div v-if="!workerRows.length" class="worker-status">
            <span class="dot" style="background: var(--txt3)" />
            <span class="worker-text">No worker has reported yet</span>
          </div>
        </template>

        <div class="side-rule" />

        <button
          class="side-item"
          :class="{ active: screen === 'settings' }"
          @click="navigate('settings')"
        >
          <span class="side-item-label">Settings</span>
        </button>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <!-- Rendered at every width, shown by CSS only below the desktop
             breakpoint — keeping it in the DOM is what lets the responsive
             layer stay pure CSS. -->
        <button
          class="nav-toggle"
          aria-label="Open navigation"
          :aria-expanded="String(navOpen)"
          @click="navOpen = !navOpen"
        >
          ☰
        </button>
        <button class="palette-trigger" @click="paletteOpen = true">
          <span class="search-glyph">⌕</span>
          <span class="palette-trigger-label">Jump to a service…</span>
          <span class="kbd">⌘K</span>
        </button>
        <div class="topbar-right">
          <!-- §6c: persistently visible, not only on the settings screen. An
               operator must never have to go looking to find out that writes
               are currently open.

               Labelled "auth mode", not "auth": this reports the SERVER's mode
               and renders identically whether or not you are signed in, so the
               shorter form reads as a claim about your own session — which is
               exactly how it was misread the first time somebody opened the app
               in an incognito window. "auth mode" also matches
               HOMELABVAULT_AUTH_MODE, which is the thing an operator edits. -->
          <button
            class="auth-chip"
            :class="{ open: state.authMode !== 'full' }"
            :title="
              state.authMode === 'full'
                ? 'This server requires a token for writes. Open settings.'
                : `This server accepts unauthenticated writes (${state.authMode} mode). Open settings.`
            "
            @click="navigate('settings')"
          >
            <span class="auth-chip-prefix">auth mode: </span>{{ state.authMode }}
          </button>
          <div class="seg">
            <button :class="{ on: theme === 'light' }" @click="theme = 'light'">Light</button>
            <button :class="{ on: theme === 'dark' }" @click="theme = 'dark'">Dark</button>
          </div>
          <!-- Navigation lives in the sidebar, and Export/API moved onto the
               settings screen. The top bar was accumulating every link in the
               app; what belongs here is state, not navigation. -->
        </div>
      </header>

      <!-- `goCatalog`, not `goHome` (validation 2026-08-07, findings 1 & 2):
           the button says "Back to the catalog", and with the dashboard as the
           landing page `goHome` made it — and the post-delete return below —
           land on the Command Center instead. The label is the contract. -->
      <!-- `section` comes from `/settings/{section}`; bare `/settings` is the
           first section (the screen defaults it). -->
      <SettingsScreen
        v-if="screen === 'settings'"
        :section="route.section"
        @back="goCatalog"
      />

      <!-- `adapter` comes from `/dashboard/{adapter}`; bare `/dashboard` is the
           Overview (the Dashboard defaults the prop to 'overview'). -->
      <Dashboard
        v-else-if="screen === 'dashboard'"
        :adapter="route.adapter"
        @filter="applyQuery"
      />

      <ServiceRecord
        v-else-if="screen === 'record' && current"
        :service="current"
        @back="goCatalog"
        @changed="showBanner('Saved.')"
      />

      <!-- A `/services/{slug}` link to something that no longer exists. Saying
           so beats silently showing the catalog, which reads as a working link
           to the wrong page. -->
      <main v-else-if="screen === 'record' && !state.loading" class="content">
        <div class="card" style="padding: 56px; text-align: center">
          <div style="font: 800 15px 'Manrope', sans-serif">No service with that address</div>
          <div style="font: 500 12.5px 'Manrope', sans-serif; color: var(--txt2); margin-top: 6px">
            <span class="mono">{{ route.slug }}</span> is not in the catalog. It may have been
            renamed or removed.
          </div>
          <button class="btn-ghost" style="margin-top: 16px" @click="goHome">
            Back to the catalog
          </button>
        </div>
      </main>

      <!-- `home` while `ui.landing_page` is in flight, and `notFound` for a path
           this app does not own. -->
      <main v-else-if="screen === 'home' || screen === 'record'" class="content">
        <div class="spinner">Loading…</div>
      </main>

      <main v-else-if="screen === 'notFound'" class="content">
        <div class="card" style="padding: 56px; text-align: center">
          <div style="font: 800 15px 'Manrope', sans-serif">Page not found</div>
          <button class="btn-ghost" style="margin-top: 16px" @click="goHome">
            Back to the catalog
          </button>
        </div>
      </main>

      <EmptyState v-else-if="isEmptyInstall" @add="addOpen = true" />

      <main v-else class="content">
        <div v-if="state.error" class="notice err">{{ state.error }}</div>
        <div v-if="banner" class="notice ok">{{ banner }}</div>

        <div class="page-head">
          <div>
            <div class="page-title">Catalog</div>
            <div class="counts">
              <span>{{ state.services.length }} {{ state.services.length === 1 ? 'service' : 'services' }}</span>
              <template v-for="s in STATUS_FILTERS" :key="s.key">
                <span class="sep">·</span>
                <button
                  class="count-filter"
                  :class="{ on: hasTerm(state.query, `state:${s.key}`) }"
                  :style="{ color: s.color }"
                  :title="`Show only ${s.label}`"
                  @click="toggleStatus(s.key)"
                >
                  {{ counts[s.key] }} {{ s.label }}
                </button>
              </template>
            </div>
          </div>
          <button class="btn-primary" style="margin-left: auto" @click="addOpen = true">
            ＋ Add service
          </button>
        </div>

        <!-- Search bar: one input, the §6 syntax, shared with the palette -->
        <div class="card searchbar">
          <div class="search-input-row">
            <span class="search-glyph">⌕</span>
            <input
              v-model="state.query"
              placeholder="Search, or try host:pve-c state:!up"
              aria-label="Search the catalog"
              @keydown.esc="clearQuery"
            />
            <span class="result-count">{{ rows.length }}</span>
            <button
              v-if="state.query"
              class="clear-btn"
              title="Clear search (Esc)"
              aria-label="Clear search"
              @click="clearQuery"
            >
              ✕
            </button>
            <button class="view-chip" @click="savingView = true">Save view</button>
          </div>

          <div v-if="state.views.length" class="chip-row">
            <span class="chip-label">Views</span>
            <button
              v-for="view in state.views"
              :key="view.id"
              class="view-chip"
              :class="{ on: activeView && activeView.id === view.id }"
              @click="applyQuery(view.query)"
            >
              {{ view.name }}
              <span class="side-count">{{ filterServices(state.services, view.query).length }}</span>
            </button>
          </div>

          <!-- Active terms, always visible while filtering. Previously the only
               way to discover you could clear a filter was to type one that
               matched nothing. -->
          <div v-if="activeTerms.length" class="chip-row">
            <span class="chip-label">Filtering</span>
            <button
              v-for="term in activeTerms"
              :key="term"
              class="view-chip on"
              :title="`Remove ${term}`"
              @click="addTerm(term)"
            >
              {{ term }} <span style="opacity: 0.6">✕</span>
            </button>
            <button class="link-btn" @click="clearQuery">Clear all</button>
          </div>

          <div v-if="tagSuggestions.length" class="chip-row">
            <span class="chip-label">Tags</span>
            <span v-for="tag in tagSuggestions" :key="tag" class="tag-chip-wrap">
              <button class="tag-chip" @click="addTerm(`tag:${tag}`)">
                {{ tag }}
              </button>
              <button
                class="tag-edit"
                :title="`Rename or remove the tag ${tag}`"
                @click.stop="editingTaxonomy = { kind: 'tag', name: tag }"
              >
                ✎
              </button>
            </span>
          </div>

          <div class="chip-row">
            <button class="link-btn" style="margin-left: auto" @click="syntaxOpen = !syntaxOpen">
              {{ syntaxOpen ? 'Hide syntax' : 'Search syntax' }}
            </button>
          </div>

          <div v-if="syntaxOpen" class="syntax-grid">
            <button
              v-for="item in SYNTAX_HELP"
              :key="item.k"
              class="syntax-item"
              @click="addTerm(item.k)"
            >
              <span class="syntax-key">{{ item.k }}</span>
              <span class="syntax-desc">{{ item.d }}</span>
            </button>
            <div style="grid-column: 1 / -1" class="field-hint">
              Terms AND together. Prefix any value with ! to negate it.
            </div>
          </div>
        </div>

        <!-- Row multi-select actions (§6b) -->
        <div v-if="state.selected.size" class="bulkbar">
          <span class="bulkbar-label">{{ state.selected.size }} selected</span>
          <button class="btn-ghost" @click="bulkTagOpen = true">＋ Add tags</button>
          <button class="btn-ghost" @click="bulkCatOpen = true">Set category</button>
          <button class="btn-ghost" :disabled="bulkBusy" @click="recheckSelected">
            {{ bulkBusy ? 'Checking…' : '↻ Recheck' }}
          </button>
          <button class="link-btn" style="margin-left: auto" @click="state.selected = new Set()">
            Clear selection
          </button>
        </div>

        <div v-if="state.loading" class="spinner" style="margin-top: 24px">Loading catalog…</div>

        <CatalogTable
          v-else-if="rows.length"
          :rows="rows"
          :total="state.services.length"
          @open="openRecord"
          @append-term="addTerm"
          @select-term="selectOne"
        />

        <div v-else class="card" style="padding: 56px; text-align: center">
          <div style="font: 800 15px 'Manrope', sans-serif">No service matches that query</div>
          <div style="font: 500 12.5px 'Manrope', sans-serif; color: var(--txt2); margin-top: 6px">
            Try removing a term, or check the syntax reference above.
          </div>
          <button class="btn-ghost" style="margin-top: 16px" @click="clearQuery">
            Clear search
          </button>
        </div>
      </main>
    </div>

    <!-- Modals -->
    <CommandPalette
      v-if="paletteOpen"
      @close="paletteOpen = false"
      @run-view="applyQuery"
      @open-record="openRecord"
    />
    <AddService
      v-if="addOpen"
      @close="addOpen = false"
      @saved="(message) => showBanner(message)"
    />
    <TaxonomyEdit
      v-if="editingTaxonomy"
      :kind="editingTaxonomy.kind"
      :name="editingTaxonomy.name"
      @close="editingTaxonomy = null"
      @done="onTaxonomyChanged"
    />
    <WriteGate
      v-if="state.gateOpen"
      :reason="state.gateReason"
      @close="cancelGate"
      @authenticated="onGateAuthenticated"
    />

    <BaseModal v-if="savingView" title="Save this view" @close="savingView = false">
      <div class="modal-body">
        <div v-if="viewError" class="notice err">{{ viewError }}</div>
        <div class="field">
          <label class="field-label">Name</label>
          <input v-model="newViewName" placeholder="Unhealthy on pve-c" @keydown.enter="saveView" />
        </div>
        <div class="field">
          <label class="field-label">Query</label>
          <input :value="state.query" class="mono" readonly />
          <div class="field-hint">A saved view is a stored query string. Nothing more.</div>
        </div>
      </div>
      <div class="modal-foot">
        <span class="spacer" />
        <button class="btn-ghost" @click="savingView = false">Cancel</button>
        <button class="btn-primary" @click="saveView">Save view</button>
      </div>
    </BaseModal>

    <BaseModal v-if="bulkTagOpen" title="Add tags to selected services" @close="bulkTagOpen = false">
      <div class="modal-body">
        <div v-if="bulkError" class="notice err">{{ bulkError }}</div>
        <div class="field">
          <label class="field-label">Tags</label>
          <TagInput
            v-model="bulkTagValue"
            :suggestions="allTags"
          />
          <div class="field-hint">
            Added to {{ state.selected.size }} selected service(s). Existing tags
            are kept.
          </div>
        </div>
      </div>
      <div class="modal-foot">
        <span class="spacer" />
        <button class="btn-ghost" @click="bulkTagOpen = false">Cancel</button>
        <button class="btn-primary" @click="applyBulkTag">Add tags</button>
      </div>
    </BaseModal>

    <BaseModal v-if="bulkCatOpen" title="Set category on selected services" @close="bulkCatOpen = false">
      <div class="modal-body">
        <div v-if="bulkError" class="notice err">{{ bulkError }}</div>
        <div class="field">
          <label class="field-label">Category</label>
          <ComboInput
            v-model="bulkCatValue"
            :suggestions="allCategories.map((c) => c.name)"
            placeholder="Media"
            @keydown.enter="applyBulkCategory"
          />
          <div class="field-hint">
            Replaces the category on {{ state.selected.size }} selected
            service(s). Leave blank to clear it.
          </div>
        </div>
      </div>
      <div class="modal-foot">
        <span class="spacer" />
        <button class="btn-ghost" @click="bulkCatOpen = false">Cancel</button>
        <button class="btn-primary" @click="applyBulkCategory">Set category</button>
      </div>
    </BaseModal>
  </div>
</template>
