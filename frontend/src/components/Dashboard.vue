<script setup>
/**
 * Command Center — v1 §8, §7d.
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
 * **Which panels exist is the server's answer too** (`payload.panels`). A panel
 * switched off in settings is absent from the response, so this component
 * cannot render it even by mistake.
 *
 * Layout and copy follow `design/HomeLab OS Command Center v1.html`, scoped by
 * §7d. Colours are CSS variables only.
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

const STATUS_TILES = [
  { key: 'up', label: 'up', color: 'var(--ok)' },
  { key: 'down', label: 'down', color: 'var(--err)' },
  { key: 'stale', label: 'stale', color: 'var(--warn)' },
  { key: 'unknown', label: 'unknown', color: 'var(--txt3)' },
  { key: 'off', label: 'no check', color: 'var(--txt3)' },
]

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
</script>

<template>
  <main class="content dash">
    <div class="dash-main">
      <div v-if="error" class="notice err">{{ error }}</div>

      <div class="page-head">
        <div>
          <div class="page-title">Command Center</div>
          <div class="counts">
            <span v-if="cluster?.name">Cluster: {{ cluster.name }} · {{ cluster.nodes_total }} nodes</span>
            <span v-else>Service health and infrastructure at a glance</span>
            <!-- Same treatment the nodes table already gives stale Proxmox
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

      <div v-if="loading" class="spinner" style="margin-top: 24px">Loading dashboard…</div>

      <template v-else>
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

        <!-- Proxmox: not configured means not rendered (§7d), so `proxmox`
             being null is a complete answer and needs no placeholder. -->
        <template v-if="proxmox">
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
    </div>

    <DetailDrawer
      v-if="selected"
      v-bind="selected"
      @close="selected = null"
    />
  </main>
</template>
