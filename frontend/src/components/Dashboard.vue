<script setup>
/**
 * Command Center — v1 §8, §7d; adapter bar TN4.
 *
 * **Every number on this screen comes from one call to `/api/v1/panels`.** §8
 * calls that the one v1 decision v2 depends on and warns that a panel fetching
 * for itself gets rebuilt, so there is deliberately no second data source here:
 * no provider call, no per-panel endpoint, and the status counts are the
 * server's rather than recounted from the loaded catalog.
 *
 * The only thing read from the store is a service's URL and location when the
 * drawer opens — a lookup in data the app already has, not a second opinion
 * about anything the panel displays.
 *
 * **The screen is an adapter bar (TN4).** A pill strip — `Overview` plus one
 * pill per *configured* provider — selects which section renders, and the
 * selection is a real bookmarkable route (`/dashboard/{adapter}`), not internal
 * tab state. The `adapter` prop is that route parameter; a bare `/dashboard`
 * (or an unknown adapter) is the curated Overview. The TrueNAS section reuses
 * the Proxmox section's components 1:1 — `.dash-card`/`.meter`/`.stat-tile`/
 * `.table` + `DetailDrawer.vue` — mapping TrueNAS data onto the same markup.
 *
 * **Which panels exist is the server's answer too** (`payload.panels` and the
 * per-sub-block toggles): a panel switched off in settings is absent from the
 * response, so this component cannot render it even by mistake. The Workspaces
 * concept (the `+` tab, catalogued-service pills, tab counts) is deliberately
 * out of scope and absent here.
 *
 * Colours are CSS variables only.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'

import { api } from '../api.js'
import {
  barWidth,
  bytes,
  bytesPair,
  load,
  percent,
  safeUrl,
  since,
  uptime,
} from '../format.js'
import { navigate } from '../router.js'
import { state } from '../store.js'
import DetailDrawer from './DetailDrawer.vue'
import StatusPill from './StatusPill.vue'

const props = defineProps({
  /** The adapter route parameter — `overview` | `proxmox` | `truenas`. */
  adapter: { type: String, default: 'overview' },
})
const emit = defineEmits(['filter'])

const payload = ref(null)
const loading = ref(true)
const error = ref('')
/** A quiet refresh failed; the dashboard on screen is the last good one —
 *  the same distinction the catalog draws with `state.offline` (audit
 *  informational: a page whose stated purpose is to sit open needs to say
 *  when it has stopped refreshing). */
const offline = ref(false)
/** Ticks so "polled 4s ago" actually counts up on a page left open. */
const clock = ref(Date.now())
const selected = ref(null)

const panels = computed(() => payload.value?.panels || [])
const summary = computed(() => payload.value?.status_summary || null)
const adapters = computed(() => payload.value?.adapters || [])
const overview = computed(() => payload.value?.overview || null)

/**
 * The active section. A known adapter renders its section; anything else — a
 * bare `/dashboard`, the `overview` pill, or a stale/unknown bookmark like
 * `/dashboard/foobar` — falls back to Overview, which is the graceful default
 * the done-when asks for.
 */
const KNOWN_ADAPTERS = ['proxmox', 'truenas']
const view = computed(() =>
  KNOWN_ADAPTERS.includes(props.adapter) ? props.adapter : 'overview',
)

function goAdapter(key) {
  navigate('dashboard', { adapter: key })
}

const TONE_VAR = { ok: 'var(--ok)', warn: 'var(--warn)', err: 'var(--err)' }
function toneVar(tone) {
  return TONE_VAR[tone] || 'var(--txt2)'
}

const STATUS_TILES = [
  { key: 'up', label: 'up', color: 'var(--ok)' },
  { key: 'down', label: 'down', color: 'var(--err)' },
  { key: 'stale', label: 'stale', color: 'var(--warn)' },
  { key: 'unknown', label: 'unknown', color: 'var(--txt3)' },
  { key: 'off', label: 'no check', color: 'var(--txt3)' },
]

// --- Proxmox ---------------------------------------------------------------

const proxmox = computed(() => payload.value?.proxmox || null)
const cluster = computed(() => proxmox.value?.cluster || null)
const nodes = computed(() => proxmox.value?.nodes || [])

const HA_LABELS = {
  enabled: 'Enabled',
  'not-configured': 'Not configured',
  unknown: 'Unknown',
}

/**
 * Cluster health as one word — the mockup's big green label.
 *
 * `quorate === null` is a standalone host, which is healthy: it has no quorum
 * to lose. Only an explicit `false` is a failure, which is why this reads the
 * value rather than its truthiness.
 */
const clusterHealth = computed(() => {
  const info = cluster.value
  if (!info) return { label: 'Unknown', colour: 'var(--txt3)' }
  if (info.quorate === false) return { label: 'No quorum', colour: 'var(--err)' }
  if (info.nodes_online < info.nodes_total) {
    return { label: 'Degraded', colour: 'var(--warn)' }
  }
  return { label: 'Healthy', colour: 'var(--ok)' }
})

// --- TrueNAS ---------------------------------------------------------------

const truenas = computed(() => payload.value?.truenas || null)
const tnSystem = computed(() => truenas.value?.system || null)
const tnPools = computed(() => truenas.value?.pools || [])
const tnDisks = computed(() => truenas.value?.disks || [])
const tnBackups = computed(() => truenas.value?.backups || [])
const tnAlerts = computed(() => truenas.value?.alerts || [])

const tnHealthyCount = computed(() => tnPools.value.filter((p) => p.healthy).length)

/** Overall pool health as one word, mirroring `clusterHealth`. */
const poolHealth = computed(() => {
  const pools = tnPools.value
  if (!pools.length) return { label: 'Unknown', colour: 'var(--txt3)' }
  if (pools.some((p) => !p.healthy)) return { label: 'Degraded', colour: 'var(--err)' }
  return { label: 'Online', colour: 'var(--ok)' }
})

/** A running scrub/resilver, if any — the Pool health card's second line. */
const tnScan = computed(
  () =>
    tnPools.value.find(
      (p) => p.scan_function && p.scan_state && p.scan_state !== 'FINISHED',
    ) || null,
)
const tnScanLabel = computed(() => {
  const scan = tnScan.value
  if (!scan) return 'Idle'
  const pct = scan.scan_percentage != null ? ` · ${percent(scan.scan_percentage, 0)}` : ''
  return `${scan.scan_function}${pct}`
})

/** Aggregate capacity across pools that report it. */
const tnCapacity = computed(() => {
  let used = 0
  let total = 0
  for (const pool of tnPools.value) {
    if (pool.used_bytes != null && pool.total_bytes) {
      used += pool.used_bytes
      total += pool.total_bytes
    }
  }
  return { used, total, pct: total ? (100 * used) / total : null }
})

/** The section's health pill, from the adapter bar's own health verdict. */
const HEALTH_PILL = {
  ok: { label: 'Healthy', tone: 'up' },
  warn: { label: 'Attention', tone: 'stale' },
  err: { label: 'Degraded', tone: 'down' },
}
const tnHealth = computed(() => {
  const found = adapters.value.find((a) => a.key === 'truenas')
  return HEALTH_PILL[found?.health] || null
})

// Which table tab is showing. Clamped to the tabs that actually exist, so a
// toggled-off sub-block can never leave the table pointed at an absent tab.
const tnTab = ref('pools')
const tnTabs = computed(() => {
  const tabs = []
  if (truenas.value?.pools) tabs.push({ key: 'pools', label: 'Pools' })
  if (truenas.value?.disks) tabs.push({ key: 'disks', label: 'Disks' })
  if (tnBackups.value.length) tabs.push({ key: 'backups', label: 'Backups' })
  if (truenas.value?.alerts) tabs.push({ key: 'alerts', label: 'Alerts' })
  return tabs
})
const activeTnTab = computed(() => {
  const keys = tnTabs.value.map((t) => t.key)
  return keys.includes(tnTab.value) ? tnTab.value : keys[0] || null
})

/** `ZpoolCapacityNotice` -> `Zpool Capacity Notice`. The public panel ships
 *  the raw `klass`; splitting it for display happens here, never server-side. */
function humanizeCategory(klass) {
  if (!klass) return 'Alert'
  return klass
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .trim()
}

const ALERT_TONE = {
  INFO: 'var(--txt3)',
  NOTICE: 'var(--txt3)',
  WARNING: 'var(--warn)',
  ERROR: 'var(--err)',
  CRITICAL: 'var(--err)',
  ALERT: 'var(--err)',
  EMERGENCY: 'var(--err)',
}
function alertColour(level) {
  return ALERT_TONE[level] || 'var(--txt3)'
}
function smartColour(status) {
  if (status === 'PASS') return 'var(--ok)'
  if (status === 'FAIL') return 'var(--err)'
  return 'var(--txt3)'
}
function backupColour(stateValue) {
  if (['FINISHED', 'SUCCESS'].includes(stateValue)) return 'var(--ok)'
  if (['ERROR', 'FAILED'].includes(stateValue)) return 'var(--err)'
  return 'var(--txt3)'
}

// --- Loading ---------------------------------------------------------------

async function loadPanels({ quiet = false } = {}) {
  if (!quiet) loading.value = true
  try {
    payload.value = await api.getPanels()
    error.value = ''
    offline.value = false
  } catch (err) {
    // A failed refresh must not blank a dashboard that is still readable —
    // same rule the catalog follows. But it must not fail silently either:
    // the numbers on screen stop meaning "now" the moment a refresh fails.
    if (!quiet || !payload.value) error.value = err.message || 'Could not load the dashboard.'
    else offline.value = true
  } finally {
    loading.value = false
    clock.value = Date.now()
  }
}

let refreshTimer = null
let clockTimer = null

function onVisibility() {
  if (document.visibilityState === 'visible') loadPanels({ quiet: true })
}

onMounted(() => {
  loadPanels()
  refreshTimer = setInterval(() => {
    if (document.visibilityState === 'visible') loadPanels({ quiet: true })
  }, 15000)
  clockTimer = setInterval(() => {
    clock.value = Date.now()
  }, 5000)
  document.addEventListener('visibilitychange', onVisibility)
})

onUnmounted(() => {
  clearInterval(refreshTimer)
  clearInterval(clockTimer)
  document.removeEventListener('visibilitychange', onVisibility)
})

/** Clicking a count filters the catalog, exactly as the catalog header does. */
function filterBy(key) {
  emit('filter', `state:${key}`)
}

// --- Drawer ----------------------------------------------------------------

/** The PVE web interface port. Proxmox's default and effectively universal. */
const PVE_WEB_PORT = 8006

function openNode(node) {
  const rows = [
    { key: 'Cluster', value: cluster.value?.name || '—' },
    { key: 'Version', value: node.version || '—' },
    { key: 'Uptime', value: uptime(node.uptime_s) },
    { key: 'CPU', value: `${percent(node.cpu_pct)} of ${node.cpu_count ?? '—'}` },
    { key: 'RAM', value: bytesPair(node.mem_used_bytes, node.mem_total_bytes) },
    { key: 'Load', value: load(node.load_avg) },
    {
      key: 'Storage',
      value: bytesPair(node.storage_used_bytes, node.storage_total_bytes),
    },
  ]
  if (node.ip) rows.push({ key: 'Address', value: node.ip })
  // Goes through the same guard as every other link in the app, even though
  // this URL is server-built rather than operator-typed. A sink is a sink.
  const console = node.ip ? safeUrl(`https://${node.ip}:${PVE_WEB_PORT}`) : null
  selected.value = {
    title: node.name,
    subtitle: 'Cluster node · local storage only',
    status: node.online
      ? { label: 'Online', tone: 'up' }
      : { label: 'Offline', tone: 'down' },
    rows,
    // Still a list, and still sometimes empty — a node whose address the
    // provider could not read gets no action rather than a dead link.
    actions: console ? [{ label: 'Open web UI', href: console }] : [],
  }
}

/**
 * A service from the attention list. Its URL and location come from the catalog
 * the app has already loaded; version and uptime come from the node its guest
 * runs on, and rows we cannot fill are omitted rather than shown as "—" for
 * every service in the catalog.
 */
function openService(row) {
  const service = state.services.find((s) => s.slug === row.slug) || {}
  const host = service.host_label || service.location_derived || ''
  const node = nodes.value.find((n) => host.startsWith(`${n.name} `) || host === n.name)
  // A service URL is operator- or agent-supplied text with no scheme validation
  // on the write path, so it is only ever linked when it is really http(s) —
  // see `safeUrl`, and the api-security finding behind it.
  const url = safeUrl(service.launch_url)
  const rows = []
  if (service.launch_url) {
    rows.push({ key: 'URL', value: service.launch_url, href: url })
  }
  if (host) rows.push({ key: 'Host', value: host })
  if (cluster.value?.name && node) rows.push({ key: 'Cluster', value: cluster.value.name })
  if (node?.version) rows.push({ key: 'Version', value: node.version })
  if (node?.uptime_s !== undefined && node?.uptime_s !== null) {
    rows.push({ key: 'Uptime', value: uptime(node.uptime_s) })
  }
  selected.value = {
    title: service.name || row.name,
    subtitle: service.category || 'Service',
    status: { label: row.status, tone: row.status },
    rows,
    actions: url ? [{ label: 'Open external', href: url }] : [],
  }
}

function openPool(pool) {
  const rows = [
    { key: 'Status', value: pool.status || '—' },
    { key: 'Capacity', value: bytesPair(pool.used_bytes, pool.total_bytes) },
    { key: 'Used', value: pool.used_pct != null ? percent(pool.used_pct) : '—' },
    { key: 'Free', value: bytes(pool.free_bytes) },
  ]
  if (pool.scan_function && pool.scan_state) {
    const pct = pool.scan_percentage != null ? ` (${percent(pool.scan_percentage, 0)})` : ''
    rows.push({ key: 'Scan', value: `${pool.scan_function} ${pool.scan_state}${pct}` })
  }
  selected.value = {
    title: pool.name,
    subtitle: 'ZFS pool',
    status: pool.healthy
      ? { label: 'Healthy', tone: 'up' }
      : { label: 'Degraded', tone: 'down' },
    rows,
    actions: [],
  }
}

function openDisk(disk) {
  const rows = [
    { key: 'Model', value: disk.model || '—' },
    { key: 'Type', value: disk.disk_type || '—' },
    { key: 'Pool', value: disk.pool || '—' },
    { key: 'Size', value: bytes(disk.size_bytes) },
    { key: 'SMART', value: disk.smart_status || 'Unknown' },
  ]
  if (disk.temperature_c != null) rows.push({ key: 'Temperature', value: `${disk.temperature_c}°C` })
  selected.value = {
    title: disk.name,
    subtitle: 'Disk',
    status:
      disk.smart_status === 'FAIL'
        ? { label: 'SMART FAIL', tone: 'down' }
        : disk.smart_status === 'PASS'
          ? { label: 'SMART PASS', tone: 'up' }
          : { label: 'SMART unknown', tone: 'unknown' },
    rows,
    actions: [],
  }
}

function openBackup(task) {
  selected.value = {
    title: task.name || task.kind,
    subtitle: task.kind === 'cloudsync' ? 'Cloud sync task' : 'Replication task',
    status: ['FINISHED', 'SUCCESS'].includes(task.state)
      ? { label: task.state, tone: 'up' }
      : ['ERROR', 'FAILED'].includes(task.state)
        ? { label: task.state, tone: 'down' }
        : { label: task.state || 'Unknown', tone: 'unknown' },
    rows: [
      { key: 'Kind', value: task.kind },
      { key: 'Last run', value: task.at ? since(task.at, clock.value) : '—' },
    ],
    actions: [],
  }
}

function openAlert(alert) {
  // The public payload carries no raw message — only level, category (klass)
  // and time. The drawer shows exactly that; the full text lives in TrueNAS.
  selected.value = {
    title: humanizeCategory(alert.category),
    subtitle: 'TrueNAS alert',
    status: { label: alert.level || 'Alert', tone: alert.level === 'WARNING' ? 'stale' : 'down' },
    rows: [
      { key: 'Level', value: alert.level || '—' },
      { key: 'Raised', value: alert.at ? since(alert.at, clock.value) : '—' },
    ],
    actions: [],
  }
}
</script>

<template>
  <main class="content dash">
    <div class="dash-main">
      <div v-if="error" class="notice err">{{ error }}</div>

      <div class="page-head">
        <div>
          <div class="page-title">Command Center</div>
          <div class="counts">
            <span>Service health and infrastructure at a glance</span>
            <!-- Same treatment the nodes table already gives stale provider
                 data: the page keeps rendering the last good payload, and
                 says so rather than looking current. -->
            <span v-if="offline" class="settings-badge warn" style="margin-left: 10px">
              Not refreshing — server unreachable
            </span>
          </div>
        </div>
        <button class="btn-ghost" style="margin-left: auto" @click="navigate('catalog')">
          Service Catalog →
        </button>
      </div>

      <!-- The adapter bar (TN4): Overview + one pill per configured provider.
           A horizontal-scroll strip on narrow widths (styles.css) — content
           navigation, never the hamburger drawer. Rendered straight from
           payload.adapters, so only configured adapters appear. -->
      <div
        v-if="adapters.length > 1 || view !== 'overview'"
        class="adapter-bar"
        role="tablist"
        aria-label="Adapters"
      >
        <button
          v-for="a in adapters"
          :key="a.key"
          class="adapter-pill"
          :class="{ active: a.key === view }"
          role="tab"
          :aria-selected="a.key === view"
          @click="goAdapter(a.key)"
        >
          <span
            v-if="a.key !== 'overview'"
            class="adapter-dot"
            :class="`dot-${a.health || 'warn'}`"
          />
          <span class="adapter-label">{{ a.label }}</span>
        </button>
      </div>

      <div v-if="loading" class="spinner" style="margin-top: 24px">Loading dashboard…</div>

      <template v-else>
        <!-- ============================ OVERVIEW ============================ -->
        <template v-if="view === 'overview'">
          <!-- Status summary (§8). First because it is the one panel that works
               with no provider configured. -->
          <section v-if="summary" class="card dash-card">
            <div class="dash-card-head">
              <div class="dash-card-title">Status Summary</div>
              <div class="dash-aside">{{ summary.total }} services</div>
            </div>
            <div class="stat-row">
              <button
                v-for="tile in STATUS_TILES"
                :key="tile.key"
                class="stat-tile"
                :title="`Show only ${tile.label} in the catalog`"
                @click="filterBy(tile.key)"
              >
                <div class="stat-value" :style="{ color: tile.color }">
                  {{ summary[tile.key] }}
                </div>
                <div class="stat-label">{{ tile.label }}</div>
              </button>
            </div>

            <div v-if="summary.attention.length" class="dash-list">
              <div class="col-head">Needs attention</div>
              <button
                v-for="row in summary.attention"
                :key="row.slug"
                class="dash-list-row"
                @click="openService(row)"
              >
                <span class="dash-list-name">{{ row.name }}</span>
                <StatusPill :service="row" :note="false" />
              </button>
              <button
                v-if="summary.attention_total > summary.attention.length"
                class="link-btn"
                @click="filterBy('!up')"
              >
                and {{ summary.attention_total - summary.attention.length }} more
              </button>
            </div>
            <div v-else class="dash-empty">Everything with a health check is up.</div>
          </section>

          <!-- Curated per-adapter tiles. Each drills into its section. Absent
               when no provider is configured — the Overview still works. -->
          <div v-if="overview && overview.adapters.length" class="dash-summary">
            <section
              v-for="ad in overview.adapters"
              :key="ad.key"
              class="card dash-card ov-card"
              role="button"
              :tabindex="0"
              @click="goAdapter(ad.key)"
              @keydown.enter="goAdapter(ad.key)"
            >
              <div class="dash-card-head">
                <div class="dash-card-title">{{ ad.label }}</div>
                <span class="adapter-dot" :class="`dot-${ad.health || 'warn'}`" />
              </div>
              <div v-for="stat in ad.stats" :key="stat.label" class="dash-split">
                <span class="dash-key">{{ stat.label }}</span>
                <span
                  class="dash-strong"
                  :style="stat.tone ? { color: toneVar(stat.tone) } : {}"
                >{{ stat.value }}</span>
              </div>
              <div class="ov-open">Open {{ ad.label }} →</div>
            </section>
          </div>
        </template>

        <!-- ============================ PROXMOX ============================ -->
        <template v-else-if="view === 'proxmox'">
          <div v-if="!proxmox" class="card dash-card dash-degraded">
            <div class="dash-card-title">Proxmox</div>
            <div class="dash-empty">Proxmox is not configured.</div>
          </div>

          <template v-else>
            <div v-if="!proxmox.available" class="card dash-card dash-degraded">
              <div class="dash-card-title">Proxmox</div>
              <div class="dash-empty">{{ proxmox.detail }}</div>
            </div>

            <template v-else>
              <div v-if="cluster" class="dash-summary">
                <section class="card dash-card">
                  <div class="col-head">Cluster health</div>
                  <div class="dash-headline" :style="{ color: clusterHealth.colour }">
                    {{ clusterHealth.label }}
                  </div>
                  <div class="dash-split">
                    <span class="dash-key">Quorum</span>
                    <span class="dash-strong">
                      {{ cluster.quorate === null ? 'Single host' : cluster.quorate ? 'Yes' : 'No' }}
                    </span>
                  </div>
                  <div class="dash-split">
                    <span class="dash-key">HA</span>
                    <span class="dash-strong">{{ HA_LABELS[cluster.ha] || cluster.ha }}</span>
                  </div>
                </section>

                <section class="card dash-card">
                  <div class="dash-card-head">
                    <div class="col-head" style="flex: 1">Compute</div>
                    <div class="dash-stamp">polled {{ since(proxmox.polled_at, clock) }}</div>
                  </div>
                  <div class="dash-big">
                    <span class="dash-big-value">{{ cluster.nodes_online }}</span>
                    <span class="dash-big-of">/ {{ cluster.nodes_total }}</span>
                  </div>
                  <div class="dash-key">Online nodes</div>
                  <div class="meter" style="margin-top: 14px">
                    <div class="meter-fill" :style="{ width: barWidth(100 * cluster.nodes_online / (cluster.nodes_total || 1)), background: 'var(--accent)' }" />
                  </div>
                  <div class="dash-meter-row">
                    <span class="dash-key" style="flex: 1">CPU usage</span>
                    <span class="dash-num" style="color: var(--accent)">{{ percent(cluster.cpu_pct) }}</span>
                    <span class="dash-of">of {{ cluster.cpu_count }} CPUs</span>
                  </div>
                  <div class="meter">
                    <div class="meter-fill" :style="{ width: barWidth(cluster.cpu_pct), background: 'var(--accent)' }" />
                  </div>
                  <div class="dash-meter-row">
                    <span class="dash-key" style="flex: 1">RAM usage</span>
                    <span class="dash-num" style="color: var(--ok)">{{ percent(cluster.mem_pct) }}</span>
                    <span class="dash-of">of {{ bytes(cluster.mem_total_bytes, 0) }}</span>
                  </div>
                  <div class="meter">
                    <div class="meter-fill" :style="{ width: barWidth(cluster.mem_pct), background: 'var(--ok)' }" />
                  </div>
                </section>

                <section class="card dash-card">
                  <div class="col-head">Storage</div>
                  <div class="dash-big">
                    <span class="dash-big-value">{{ cluster.storages_online }}</span>
                    <span class="dash-big-of">/ {{ cluster.storages_total }}</span>
                  </div>
                  <div class="dash-key">Online storages</div>
                  <div class="dash-split">
                    <span class="dash-key">Total capacity</span>
                    <!-- Stated, not hidden: shared storages are counted once each,
                         and several NFS exports of one pool each report that pool's
                         size. Proxmox's own datacenter summary does the same. -->
                    <span
                      class="dash-num"
                      title="Shared storages are counted once. Separate exports of one NAS pool each report that pool's size, so a multi-export NAS inflates this total — the same limit Proxmox's datacenter summary has."
                    >
                      {{ bytes(cluster.storage_total_bytes) }}
                    </span>
                  </div>
                  <div class="dash-split">
                    <span class="dash-key">Used</span>
                    <span class="dash-num">
                      {{ bytes(cluster.storage_used_bytes) }} ({{ percent(cluster.storage_pct) }})
                    </span>
                  </div>
                  <div class="meter">
                    <div class="meter-fill grad" :style="{ width: barWidth(cluster.storage_pct) }" />
                  </div>
                  <div class="dash-counts">
                    <span class="dash-key">VMs</span><span class="dash-num">{{ cluster.vm_count }}</span>
                    <span class="dash-key" style="margin-left: 22px">LXC</span>
                    <span class="dash-num">{{ cluster.lxc_count }}</span>
                  </div>
                </section>
              </div>

              <!-- Nodes table: exactly §7d's columns, in the mockup's order. -->
              <section v-if="proxmox.nodes" class="card table nodes-table">
                <div class="dash-card-head">
                  <div class="dash-card-title">Nodes</div>
                  <div class="dash-aside">
                    {{ proxmox.nodes.filter((n) => n.online).length }} online
                  </div>
                  <div v-if="proxmox.stale" class="settings-badge warn" style="margin-left: auto">
                    Not refreshing
                  </div>
                </div>
                <div class="row node-row head">
                  <div class="col-head">Node</div>
                  <div class="col-head">Status</div>
                  <div class="col-head">Uptime</div>
                  <div class="col-head">CPU</div>
                  <div class="col-head">RAM</div>
                  <div class="col-head">Load avg</div>
                  <div class="col-head">Storage</div>
                  <div class="col-head">Version</div>
                </div>
                <!-- The phone layout labels these cells with :nth-child ::before
                     rules (styles.css, responsive) because uptime/load/storage/
                     version share one class. That coupling means the eight cells
                     below must stay unconditional and in this order — if one ever
                     becomes v-if'd, the phone labels shift onto the wrong cells. -->
                <button
                  v-for="node in nodes"
                  :key="node.name"
                  class="row node-row body"
                  @click="openNode(node)"
                >
                  <span class="node-name">{{ node.name }}</span>
                  <span class="pill" :class="node.online ? 'st-up' : 'st-down'">
                    <span class="pill-dot" />
                    <span class="pill-label">{{ node.online ? 'Online' : 'Offline' }}</span>
                  </span>
                  <span class="node-cell">{{ uptime(node.uptime_s) }}</span>
                  <span class="node-meter">
                    <span class="node-cell fixed">{{ percent(node.cpu_pct, 0) }}</span>
                    <span class="meter thin">
                      <span class="meter-fill" :style="{ width: barWidth(node.cpu_pct), background: 'var(--accent)' }" />
                    </span>
                  </span>
                  <span class="node-meter">
                    <span class="node-cell fixed">
                      {{ node.mem_total_bytes ? percent(100 * node.mem_used_bytes / node.mem_total_bytes, 0) : '—' }}
                    </span>
                    <span class="meter thin">
                      <span
                        class="meter-fill"
                        :style="{
                          width: barWidth(node.mem_total_bytes ? (100 * node.mem_used_bytes) / node.mem_total_bytes : 0),
                          background: 'var(--ok)',
                        }"
                      />
                    </span>
                  </span>
                  <span class="node-cell">{{ load(node.load_avg) }}</span>
                  <span class="node-cell">
                    {{ bytesPair(node.storage_used_bytes, node.storage_total_bytes) }}
                  </span>
                  <span class="node-cell">{{ node.version || '—' }}</span>
                </button>
              </section>
            </template>
          </template>
        </template>

        <!-- ============================ TRUENAS ============================ -->
        <template v-else-if="view === 'truenas'">
          <div v-if="!truenas" class="card dash-card dash-degraded">
            <div class="dash-card-title">TrueNAS</div>
            <div class="dash-empty">TrueNAS is not configured.</div>
          </div>

          <template v-else>
            <!-- Section header: identity + a health pill, mirroring the Proxmox
                 cluster header. No Web UI link — the public panel does not carry
                 TRUENAS_HOST (unlike Proxmox node IPs); see BUILD_LOG. -->
            <div class="tn-head">
              <div class="dash-card-title">
                TrueNAS<span v-if="truenas.hostname" class="tn-host"> · {{ truenas.hostname }}</span>
              </div>
              <span v-if="tnHealth" class="pill" :class="`st-${tnHealth.tone}`">
                <span class="pill-dot" />
                <span class="pill-label">{{ tnHealth.label }}</span>
              </span>
              <div v-if="truenas.available && truenas.stale" class="settings-badge warn">
                Not refreshing
              </div>
            </div>

            <div v-if="!truenas.available" class="card dash-card dash-degraded">
              <div class="dash-empty">{{ truenas.detail }}</div>
            </div>

            <template v-else>
              <div class="dash-summary">
                <!-- Pool health + Capacity ride the pools toggle (both derive
                     from pools); System rides the system toggle. -->
                <section v-if="truenas.pools" class="card dash-card">
                  <div class="col-head">Pool health</div>
                  <div class="dash-headline" :style="{ color: poolHealth.colour }">
                    {{ poolHealth.label }}
                  </div>
                  <div class="dash-split">
                    <span class="dash-key">Healthy pools</span>
                    <span class="dash-strong">{{ tnHealthyCount }} / {{ tnPools.length }}</span>
                  </div>
                  <div class="dash-split">
                    <span class="dash-key">Scrub / resilver</span>
                    <span class="dash-strong">{{ tnScanLabel }}</span>
                  </div>
                </section>

                <section v-if="tnSystem" class="card dash-card">
                  <div class="dash-card-head">
                    <div class="col-head" style="flex: 1">System</div>
                    <div class="dash-stamp">polled {{ since(truenas.polled_at, clock) }}</div>
                  </div>
                  <div class="dash-big">
                    <span class="dash-big-value">{{ load(tnSystem.load_avg) }}</span>
                  </div>
                  <div class="dash-key">Load average (1m)</div>
                  <div class="dash-meter-row" style="margin-top: 14px">
                    <span class="dash-key" style="flex: 1">RAM</span>
                    <span class="dash-num" style="color: var(--ok)">
                      {{ tnSystem.mem_pct != null ? percent(tnSystem.mem_pct) : bytes(tnSystem.mem_total_bytes, 0) }}
                    </span>
                    <span v-if="tnSystem.mem_pct != null" class="dash-of">
                      of {{ bytes(tnSystem.mem_total_bytes, 0) }}
                    </span>
                  </div>
                  <div v-if="tnSystem.mem_pct != null" class="meter">
                    <div class="meter-fill" :style="{ width: barWidth(tnSystem.mem_pct), background: 'var(--ok)' }" />
                  </div>
                  <div class="dash-split">
                    <span class="dash-key">Uptime</span>
                    <span class="dash-strong">{{ uptime(tnSystem.uptime_s) }}</span>
                  </div>
                  <div class="dash-split">
                    <span class="dash-key">Version</span>
                    <span class="dash-strong">{{ tnSystem.version || '—' }}</span>
                  </div>
                </section>

                <section v-if="truenas.pools" class="card dash-card">
                  <div class="col-head">Capacity</div>
                  <div class="dash-split">
                    <span class="dash-key">Used</span>
                    <span class="dash-num">
                      {{ bytes(tnCapacity.used) }} ({{ tnCapacity.pct != null ? percent(tnCapacity.pct) : '—' }})
                    </span>
                  </div>
                  <div class="dash-split">
                    <span class="dash-key">Total</span>
                    <span class="dash-num">{{ bytes(tnCapacity.total) }}</span>
                  </div>
                  <div class="meter">
                    <div class="meter-fill grad" :style="{ width: barWidth(tnCapacity.pct) }" />
                  </div>
                  <div v-for="pool in tnPools" :key="pool.name" class="dash-meter-row">
                    <span class="dash-key" style="flex: 1">{{ pool.name }}</span>
                    <span class="dash-num">{{ bytesPair(pool.used_bytes, pool.total_bytes) }}</span>
                  </div>
                </section>
              </div>

              <!-- Tabbed table (Pools · Disks · Backups · Alerts), reusing the
                   nodes-table markup. Rows open the shared DetailDrawer. -->
              <section v-if="tnTabs.length" class="card table truenas-table">
                <div class="dash-tabs" role="tablist" aria-label="TrueNAS details">
                  <button
                    v-for="tab in tnTabs"
                    :key="tab.key"
                    class="dash-tab"
                    :class="{ active: tab.key === activeTnTab }"
                    role="tab"
                    :aria-selected="tab.key === activeTnTab"
                    @click="tnTab = tab.key"
                  >
                    {{ tab.label }}
                  </button>
                </div>

                <!-- Pools -->
                <template v-if="activeTnTab === 'pools'">
                  <div class="row tn-pool-row head">
                    <div class="col-head">Pool</div>
                    <div class="col-head">Status</div>
                    <div class="col-head">Capacity</div>
                    <div class="col-head">Used</div>
                    <div class="col-head">Scrub</div>
                  </div>
                  <button
                    v-for="pool in tnPools"
                    :key="pool.name"
                    class="row tn-pool-row body tn-row"
                    @click="openPool(pool)"
                  >
                    <span class="node-name">{{ pool.name }}</span>
                    <span class="pill" :class="pool.healthy ? 'st-up' : 'st-down'">
                      <span class="pill-dot" />
                      <span class="pill-label">{{ pool.status || (pool.healthy ? 'Online' : 'Degraded') }}</span>
                    </span>
                    <span class="node-cell">{{ bytesPair(pool.used_bytes, pool.total_bytes) }}</span>
                    <span class="node-cell">{{ pool.used_pct != null ? percent(pool.used_pct) : '—' }}</span>
                    <span class="node-cell">
                      {{ pool.scan_state && pool.scan_state !== 'FINISHED' ? `${pool.scan_function} ${percent(pool.scan_percentage, 0)}` : '—' }}
                    </span>
                  </button>
                </template>

                <!-- Disks -->
                <template v-else-if="activeTnTab === 'disks'">
                  <div class="row tn-disk-row head">
                    <div class="col-head">Disk</div>
                    <div class="col-head">Model</div>
                    <div class="col-head">Type</div>
                    <div class="col-head">Pool</div>
                    <div class="col-head">SMART</div>
                    <div class="col-head">Temp</div>
                  </div>
                  <button
                    v-for="disk in tnDisks"
                    :key="disk.name"
                    class="row tn-disk-row body tn-row"
                    @click="openDisk(disk)"
                  >
                    <span class="node-name">{{ disk.name }}</span>
                    <span class="node-cell">{{ disk.model || '—' }}</span>
                    <span class="node-cell">{{ disk.disk_type || '—' }}</span>
                    <span class="node-cell">{{ disk.pool || '—' }}</span>
                    <span class="node-cell" :style="{ color: smartColour(disk.smart_status) }">
                      {{ disk.smart_status || '—' }}
                    </span>
                    <span class="node-cell">{{ disk.temperature_c != null ? `${disk.temperature_c}°C` : '—' }}</span>
                  </button>
                </template>

                <!-- Backups -->
                <template v-else-if="activeTnTab === 'backups'">
                  <div class="row tn-backup-row head">
                    <div class="col-head">Task</div>
                    <div class="col-head">Kind</div>
                    <div class="col-head">State</div>
                    <div class="col-head">Last run</div>
                  </div>
                  <button
                    v-for="(task, index) in tnBackups"
                    :key="`${task.kind}-${task.name}-${index}`"
                    class="row tn-backup-row body tn-row"
                    @click="openBackup(task)"
                  >
                    <span class="node-name">{{ task.name || '—' }}</span>
                    <span class="node-cell">{{ task.kind }}</span>
                    <span class="node-cell" :style="{ color: backupColour(task.state) }">
                      {{ task.state || '—' }}
                    </span>
                    <span class="node-cell">{{ task.at ? since(task.at, clock) : '—' }}</span>
                  </button>
                </template>

                <!-- Alerts — the operator's headline want. Level-coloured pill +
                     coarsened category + time. No raw message: the public panel
                     never carries it. -->
                <template v-else-if="activeTnTab === 'alerts'">
                  <div v-if="!tnAlerts.length" class="dash-empty" style="padding: 22px">
                    No active alerts.
                  </div>
                  <button
                    v-for="alert in tnAlerts"
                    :key="alert.category + alert.at"
                    class="row tn-alert-row body tn-row"
                    @click="openAlert(alert)"
                  >
                    <span class="pill" :style="{ borderColor: alertColour(alert.level), color: alertColour(alert.level) }">
                      <span class="pill-dot" :style="{ background: alertColour(alert.level) }" />
                      <span class="pill-label">{{ alert.level }}</span>
                    </span>
                    <span class="tn-alert-msg">{{ humanizeCategory(alert.category) }}</span>
                    <span class="node-cell">{{ alert.at ? since(alert.at, clock) : '—' }}</span>
                  </button>
                </template>
              </section>
            </template>
          </template>
        </template>
      </template>
    </div>

    <DetailDrawer
      v-if="selected"
      v-bind="selected"
      @close="selected = null"
    />
  </main>
</template>
