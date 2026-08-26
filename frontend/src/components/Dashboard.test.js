/**
 * The Command Center — v1 §8, §7d; adapter bar TN4.
 *
 * The invariants worth asserting from this side, none of them "the numbers
 * render":
 *
 * 1. **The panel takes its data from one endpoint.** §8 warns that a panel
 *    fetching for itself gets rebuilt, so the test that matters is that a
 *    payload without a panel produces a page without it — the frontend never
 *    fills a gap from somewhere else.
 * 2. **The drawer's actions are a list** (§7d, §12). Proven from both ends: a
 *    service has one, a node has none, and the empty case still renders.
 * 3. **The adapter bar routes, and the section renders only what the schema
 *    published.** The TrueNAS alert row shows the coarsened category, never a
 *    raw message — the payload does not even carry one.
 *
 * The router is mocked so a pill/tile click is asserted as a `navigate(...)`
 * call; real route resolution is `router.test.js`.
 */

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api.js'
import { navigate } from '../router.js'
import { state } from '../store.js'
import Dashboard from './Dashboard.vue'

vi.mock('../router.js', () => ({ navigate: vi.fn() }))

const NODES = [
  {
    name: 'pve-d',
    online: true,
    ip: '192.0.2.11',
    uptime_s: 609946,
    cpu_pct: 1.8,
    cpu_count: 16,
    mem_used_bytes: 10 * 1024 ** 3,
    mem_total_bytes: 64 * 1024 ** 3,
    load_avg: 0.38,
    storage_used_bytes: 64 * 1024 ** 3,
    storage_total_bytes: 1024 ** 4,
    version: '9.1.1',
  },
  {
    name: 'pve-b',
    online: false,
    ip: '192.0.2.12',
    uptime_s: null,
    cpu_pct: null,
    cpu_count: 20,
    mem_used_bytes: null,
    mem_total_bytes: null,
    load_avg: null,
    storage_used_bytes: null,
    storage_total_bytes: null,
    version: '',
  },
]

const CLUSTER = {
  name: 'demo-lab',
  quorate: true,
  ha: 'not-configured',
  nodes_online: 1,
  nodes_total: 2,
  cpu_pct: 1.8,
  cpu_count: 16,
  mem_used_bytes: 10 * 1024 ** 3,
  mem_total_bytes: 64 * 1024 ** 3,
  mem_pct: 15.6,
  storages_online: 5,
  storages_total: 5,
  storage_used_bytes: 4 * 1024 ** 4,
  storage_total_bytes: 45 * 1024 ** 4,
  storage_pct: 8.9,
  vm_count: 1,
  lxc_count: 36,
}

const SUMMARY = {
  total: 8,
  up: 6,
  down: 1,
  stale: 1,
  unknown: 0,
  off: 0,
  attention: [
    { slug: 'jellyfin', name: 'Jellyfin', status: 'down' },
    { slug: 'grafana', name: 'Grafana', status: 'stale' },
  ],
  attention_total: 2,
}

const TN_SYSTEM = {
  version: 'TrueNAS-SCALE-24.10.1',
  hostname: 'truenas',
  uptime_s: 864000,
  mem_used_bytes: null,
  mem_total_bytes: 64 * 1024 ** 3,
  mem_pct: null,
  load_avg: 0.52,
  cores: 12,
}

const TN_POOLS = [
  {
    name: 'tank', status: 'ONLINE', healthy: true,
    used_bytes: 6 * 1024 ** 4, total_bytes: 10 * 1024 ** 4, free_bytes: 4 * 1024 ** 4,
    used_pct: 60.0, scan_function: '', scan_state: '', scan_percentage: null,
  },
  {
    name: 'backup', status: 'DEGRADED', healthy: false,
    used_bytes: 3 * 1024 ** 4, total_bytes: 4 * 1024 ** 4, free_bytes: 1 * 1024 ** 4,
    used_pct: 75.0, scan_function: 'SCRUB', scan_state: 'SCANNING', scan_percentage: 42.5,
  },
]

const TN_DISKS = [
  { name: 'sda', model: 'WD40EFRX', disk_type: 'HDD', size_bytes: 4 * 1024 ** 4, pool: 'tank', temperature_c: 34, smart_status: 'PASS' },
  { name: 'sdc', model: 'ST4000VN008', disk_type: 'HDD', size_bytes: 4 * 1024 ** 4, pool: '', temperature_c: 41, smart_status: 'FAIL' },
]

const TN_BACKUPS = [
  { kind: 'replication', name: 'tank-to-backup', state: 'FINISHED', at: new Date().toISOString() },
  { kind: 'cloudsync', name: 'backblaze-offsite', state: 'FAILED', at: new Date().toISOString() },
]

// No `message` field on purpose — the public panel coarsens it away. The row
// carries only level, category (the klass) and time.
const TN_ALERTS = [
  { level: 'CRITICAL', category: 'ZpoolCapacityNotice', at: new Date().toISOString() },
]

const TRUENAS = {
  available: true, detail: '', polled_at: new Date().toISOString(), stale: false,
  hostname: 'truenas', system: TN_SYSTEM, pools: TN_POOLS, disks: TN_DISKS,
  backups: TN_BACKUPS, alerts: TN_ALERTS,
}

const ADAPTERS = [
  { key: 'overview', label: 'Overview', health: '' },
  { key: 'proxmox', label: 'Proxmox', health: 'warn' },
  { key: 'truenas', label: 'TrueNAS', health: 'err' },
]

const OVERVIEW = {
  adapters: [
    { key: 'proxmox', label: 'Proxmox', health: 'warn', stats: [
      { label: 'Nodes online', value: '1 / 2', tone: 'warn' },
      { label: 'Cluster', value: 'Degraded', tone: 'warn' },
    ] },
    { key: 'truenas', label: 'TrueNAS', health: 'err', stats: [
      { label: 'Pools healthy', value: '1 / 2', tone: 'err' },
      { label: 'Capacity used', value: '64%', tone: 'ok' },
      { label: 'Critical alerts', value: '1', tone: 'err' },
    ] },
  ],
}

function payload(overrides = {}) {
  return {
    landing_page: 'dashboard',
    panels: [
      'status_summary', 'proxmox_cluster', 'proxmox_nodes',
      'truenas_system', 'truenas_pools', 'truenas_disks', 'truenas_alerts',
    ],
    adapters: ADAPTERS,
    overview: OVERVIEW,
    status_summary: SUMMARY,
    proxmox: {
      available: true, detail: '', polled_at: new Date().toISOString(),
      stale: false, cluster: CLUSTER, nodes: NODES,
    },
    truenas: TRUENAS,
    ...overrides,
  }
}

async function open(body = payload(), adapter = 'overview') {
  vi.spyOn(api, 'getPanels').mockResolvedValue(body)
  const wrapper = mount(Dashboard, { props: { adapter } })
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  vi.restoreAllMocks()
  navigate.mockClear()
  state.services = []
})

describe('the panel endpoint is the only source', () => {
  it('reads the dashboard with a single call', async () => {
    const wrapper = await open()
    expect(api.getPanels).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('renders no status panel when it is switched off', async () => {
    const body = payload({ status_summary: null })
    body.panels = body.panels.filter((p) => p !== 'status_summary')
    const wrapper = await open(body, 'overview')
    expect(wrapper.text()).not.toContain('Status Summary')
    wrapper.unmount()
  })

  it('renders no nodes table when that panel alone is off', async () => {
    const body = payload()
    body.proxmox.nodes = null
    const wrapper = await open(body, 'proxmox')
    expect(wrapper.text()).toContain('Cluster health')
    expect(wrapper.find('.nodes-table').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('the adapter bar (TN4)', () => {
  it('renders a pill per configured adapter, Overview first, from the payload', async () => {
    const wrapper = await open()
    const labels = wrapper.findAll('.adapter-pill').map((p) => p.text().trim())
    expect(labels).toEqual(['Overview', 'Proxmox', 'TrueNAS'])
    wrapper.unmount()
  })

  it('marks the active adapter and puts a health dot on providers, not Overview', async () => {
    const wrapper = await open(payload(), 'truenas')
    const pills = wrapper.findAll('.adapter-pill')
    // Overview has no dot; the two providers do.
    expect(pills[0].find('.adapter-dot').exists()).toBe(false)
    expect(pills[2].find('.adapter-dot.dot-err').exists()).toBe(true)
    expect(pills[2].classes()).toContain('active')
    wrapper.unmount()
  })

  it('routes to the adapter section on click', async () => {
    const wrapper = await open()
    await wrapper.findAll('.adapter-pill')[2].trigger('click')
    expect(navigate).toHaveBeenCalledWith('dashboard', { adapter: 'truenas' })
    wrapper.unmount()
  })

  it('shows only configured adapters — one provider yields two pills', async () => {
    const body = payload({
      adapters: [
        { key: 'overview', label: 'Overview', health: '' },
        { key: 'truenas', label: 'TrueNAS', health: 'err' },
      ],
      proxmox: null,
    })
    const wrapper = await open(body, 'truenas')
    const labels = wrapper.findAll('.adapter-pill').map((p) => p.text().trim())
    expect(labels).toEqual(['Overview', 'TrueNAS'])
    wrapper.unmount()
  })
})

describe('the Overview branch', () => {
  it('shows the status summary and a curated tile per adapter', async () => {
    const wrapper = await open()
    expect(wrapper.text()).toContain('Status Summary')
    const tiles = wrapper.findAll('.ov-card')
    expect(tiles).toHaveLength(2)
    expect(wrapper.text()).toContain('Pools healthy')
    expect(wrapper.text()).toContain('Critical alerts')
    wrapper.unmount()
  })

  it('drills into a section when a tile is clicked', async () => {
    const wrapper = await open()
    await wrapper.findAll('.ov-card')[1].trigger('click')
    expect(navigate).toHaveBeenCalledWith('dashboard', { adapter: 'truenas' })
    wrapper.unmount()
  })

  it('works with no provider configured, like the status panel', async () => {
    const wrapper = await open(
      payload({
        adapters: [{ key: 'overview', label: 'Overview', health: '' }],
        overview: { adapters: [] },
        proxmox: null,
        truenas: null,
      }),
      'overview',
    )
    expect(wrapper.text()).toContain('Status Summary')
    expect(wrapper.findAll('.ov-card')).toHaveLength(0)
    wrapper.unmount()
  })
})

describe('the staleness affordance (audit informational)', () => {
  // The page's stated purpose is to sit open. A quiet refresh failure used to
  // set no state at all, so every number silently stopped meaning "now".
  it('says the dashboard has stopped refreshing, without blanking it', async () => {
    const wrapper = await open()
    api.getPanels.mockRejectedValue(new Error('unreachable'))

    document.dispatchEvent(new Event('visibilitychange'))
    await new Promise((resolve) => setTimeout(resolve, 0))
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('Not refreshing — server unreachable')
    expect(wrapper.text()).toContain('Status Summary')
    expect(wrapper.find('.notice.err').exists()).toBe(false)
    wrapper.unmount()
  })

  it('clears the flag the moment a refresh succeeds', async () => {
    const wrapper = await open()
    api.getPanels.mockRejectedValueOnce(new Error('unreachable'))

    document.dispatchEvent(new Event('visibilitychange'))
    await new Promise((resolve) => setTimeout(resolve, 0))
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('Not refreshing — server unreachable')

    document.dispatchEvent(new Event('visibilitychange'))
    await new Promise((resolve) => setTimeout(resolve, 0))
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).not.toContain('Not refreshing — server unreachable')
    wrapper.unmount()
  })
})

describe('the Proxmox section', () => {
  it('states quorum and HA, not just a colour', async () => {
    const wrapper = await open(payload(), 'proxmox')
    expect(wrapper.text()).toContain('Quorum')
    expect(wrapper.text()).toContain('Not configured')
    wrapper.unmount()
  })

  it('reads Degraded while a node is offline', async () => {
    const wrapper = await open(payload(), 'proxmox')
    expect(wrapper.text()).toContain('Degraded')
    wrapper.unmount()
  })

  it('says Healthy on a standalone host, which has no quorum to lose', async () => {
    const body = payload()
    body.proxmox.cluster = { ...CLUSTER, quorate: null, nodes_online: 1, nodes_total: 1 }
    const wrapper = await open(body, 'proxmox')
    expect(wrapper.text()).toContain('Healthy')
    expect(wrapper.text()).toContain('Single host')
    wrapper.unmount()
  })

  it('has exactly the columns §7d names, in order', async () => {
    const wrapper = await open(payload(), 'proxmox')
    const headers = wrapper.findAll('.node-row.head .col-head').map((cell) => cell.text())
    expect(headers).toEqual([
      'Node', 'Status', 'Uptime', 'CPU', 'RAM', 'Load avg', 'Storage', 'Version',
    ])
    wrapper.unmount()
  })

  it('still lists a node whose per-node read failed', async () => {
    const wrapper = await open(payload(), 'proxmox')
    const text = wrapper.find('.nodes-table').text()
    expect(text).toContain('pve-b')
    expect(text).toContain('Offline')
    wrapper.unmount()
  })

  it('shows a not-configured card when navigated to with no Proxmox', async () => {
    const wrapper = await open(payload({ proxmox: null }), 'proxmox')
    expect(wrapper.text()).toContain('Proxmox is not configured.')
    expect(wrapper.find('.nodes-table').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the reason a cluster is unavailable', async () => {
    const wrapper = await open(
      payload({
        proxmox: {
          available: false,
          detail: 'Proxmox polling is switched off in Settings.',
          polled_at: null, stale: false, cluster: null, nodes: null,
        },
      }),
      'proxmox',
    )
    expect(wrapper.text()).toContain('Proxmox polling is switched off in Settings.')
    expect(wrapper.find('.nodes-table').exists()).toBe(false)
    wrapper.unmount()
  })

  it('marks a snapshot that stopped refreshing', async () => {
    const body = payload()
    body.proxmox.stale = true
    const wrapper = await open(body, 'proxmox')
    expect(wrapper.text()).toContain('Not refreshing')
    wrapper.unmount()
  })
})

describe('the TrueNAS section', () => {
  it('renders system, pools, disks and alerts from the payload', async () => {
    const wrapper = await open(payload(), 'truenas')
    expect(wrapper.text()).toContain('Pool health')
    expect(wrapper.text()).toContain('System')
    expect(wrapper.text()).toContain('Capacity')
    // Pools tab is first: both pools listed.
    const table = wrapper.find('.truenas-table').text()
    expect(table).toContain('tank')
    expect(table).toContain('backup')
    wrapper.unmount()
  })

  it('switches to the Disks tab and shows disks with no serial column', async () => {
    const wrapper = await open(payload(), 'truenas')
    const tabs = wrapper.findAll('.dash-tab')
    await tabs.find((t) => t.text() === 'Disks').trigger('click')
    const headers = wrapper.findAll('.tn-disk-row.head .col-head').map((c) => c.text())
    expect(headers).toEqual(['Disk', 'Model', 'Type', 'Pool', 'SMART', 'Temp'])
    expect(headers).not.toContain('Serial')
    expect(wrapper.find('.truenas-table').text()).toContain('sdc')
    wrapper.unmount()
  })

  it('shows the coarsened alert — level and category, never a raw message', async () => {
    const wrapper = await open(payload(), 'truenas')
    await wrapper.findAll('.dash-tab').find((t) => t.text() === 'Alerts').trigger('click')
    const row = wrapper.find('.tn-alert-row.body')
    expect(row.text()).toContain('CRITICAL')
    // The klass is humanised for display.
    expect(row.text()).toContain('Zpool Capacity Notice')
    wrapper.unmount()
  })

  it('opens the drawer from a pool row', async () => {
    const wrapper = await open(payload(), 'truenas')
    await wrapper.find('.tn-pool-row.body').trigger('click')
    const drawer = wrapper.find('.drawer')
    expect(drawer.exists()).toBe(true)
    expect(drawer.text()).toContain('tank')
    wrapper.unmount()
  })

  it('opens the drawer from an alert row with no raw message', async () => {
    const wrapper = await open(payload(), 'truenas')
    await wrapper.findAll('.dash-tab').find((t) => t.text() === 'Alerts').trigger('click')
    await wrapper.find('.tn-alert-row.body').trigger('click')
    const drawer = wrapper.find('.drawer')
    expect(drawer.text()).toContain('Zpool Capacity Notice')
    expect(drawer.text()).toContain('CRITICAL')
    wrapper.unmount()
  })

  it('drops a sub-block whose toggle is off', async () => {
    const body = payload()
    body.truenas = { ...TRUENAS, disks: null }
    body.panels = body.panels.filter((p) => p !== 'truenas_disks')
    const wrapper = await open(body, 'truenas')
    const tabs = wrapper.findAll('.dash-tab').map((t) => t.text())
    expect(tabs).not.toContain('Disks')
    expect(tabs).toContain('Pools')
    wrapper.unmount()
  })

  it('shows a not-configured card when navigated to with no TrueNAS', async () => {
    const wrapper = await open(payload({ truenas: null }), 'truenas')
    expect(wrapper.text()).toContain('TrueNAS is not configured.')
    wrapper.unmount()
  })

  it('shows the reason when TrueNAS is unavailable', async () => {
    const wrapper = await open(
      payload({
        truenas: {
          available: false,
          detail: 'TrueNAS polling is switched off in Settings.',
          polled_at: null, stale: false, hostname: '',
          system: null, pools: null, disks: null, backups: null, alerts: null,
        },
      }),
      'truenas',
    )
    expect(wrapper.text()).toContain('TrueNAS polling is switched off in Settings.')
    expect(wrapper.find('.truenas-table').exists()).toBe(false)
    wrapper.unmount()
  })

  it('says so when there are no active alerts', async () => {
    const body = payload()
    body.truenas = { ...TRUENAS, alerts: [] }
    const wrapper = await open(body, 'truenas')
    await wrapper.findAll('.dash-tab').find((t) => t.text() === 'Alerts').trigger('click')
    expect(wrapper.find('.truenas-table').text()).toContain('No active alerts.')
    wrapper.unmount()
  })
})

describe('the detail drawer', () => {
  it('opens from a node row with that node’s numbers', async () => {
    const wrapper = await open(payload(), 'proxmox')
    await wrapper.findAll('.node-row.body')[0].trigger('click')
    const drawer = wrapper.find('.drawer')
    expect(drawer.exists()).toBe(true)
    expect(drawer.text()).toContain('pve-d')
    expect(drawer.text()).toContain('9.1.1')
    expect(drawer.text()).toContain('7d 01:25')
    wrapper.unmount()
  })

  it('offers an Open web UI action pointing at that node', async () => {
    const wrapper = await open(payload(), 'proxmox')
    await wrapper.findAll('.node-row.body')[0].trigger('click')
    const actions = wrapper.findAll('.drawer-action')
    expect(actions).toHaveLength(1)
    expect(actions[0].text()).toContain('Open web UI')
    expect(actions[0].attributes('href')).toBe('https://192.0.2.11:8006')
    wrapper.unmount()
  })

  it('renders the actions list empty rather than hiding it', async () => {
    const body = payload()
    body.proxmox.nodes = [{ ...NODES[0], ip: '' }]
    const wrapper = await open(body, 'proxmox')
    await wrapper.findAll('.node-row.body')[0].trigger('click')
    expect(wrapper.find('.drawer').text()).toContain('No actions')
    expect(wrapper.findAll('.drawer-action')).toHaveLength(0)
    wrapper.unmount()
  })

  it('gives a service the one action it has, from the same list', async () => {
    state.services = [
      {
        slug: 'jellyfin', name: 'Jellyfin', launch_url: 'https://jellyfin.lab.local',
        host_label: 'pve-d · CT 105', category: 'media',
      },
    ]
    const wrapper = await open(payload(), 'overview')
    await wrapper.findAll('.dash-list-row')[0].trigger('click')
    const actions = wrapper.findAll('.drawer-action')
    expect(actions).toHaveLength(1)
    expect(actions[0].text()).toContain('Open external')
    expect(actions[0].attributes('href')).toBe('https://jellyfin.lab.local')
    wrapper.unmount()
  })

  it('will not link a javascript: URL, and offers no action for one', async () => {
    // api-security gate, V1.5. `launch_url` has no scheme validation on the
    // write path and is anonymously settable under `open-writes`; an anchor
    // executes `javascript:` on click in every browser.
    state.services = [
      { slug: 'jellyfin', name: 'Jellyfin', launch_url: 'javascript:alert(document.cookie)' },
    ]
    const wrapper = await open(payload(), 'overview')
    await wrapper.findAll('.dash-list-row')[0].trigger('click')
    const drawer = wrapper.find('.drawer')
    expect(drawer.text()).toContain('javascript:alert(document.cookie)')
    expect(drawer.findAll('a')).toHaveLength(0)
    expect(drawer.findAll('.drawer-action')).toHaveLength(0)
    wrapper.unmount()
  })

  it('omits rows it cannot fill instead of printing an em dash for each', async () => {
    state.services = [
      { slug: 'jellyfin', name: 'Jellyfin', launch_url: 'https://jellyfin.lab.local' },
    ]
    const wrapper = await open(payload(), 'overview')
    await wrapper.findAll('.dash-list-row')[0].trigger('click')
    const keys = wrapper.findAll('.drawer-key').map((cell) => cell.text())
    expect(keys).toContain('URL')
    expect(keys).not.toContain('Version')
    expect(keys).not.toContain('Uptime')
    wrapper.unmount()
  })

  it('says so when nothing needs attention', async () => {
    const wrapper = await open(
      payload({
        status_summary: { ...SUMMARY, down: 0, stale: 0, attention: [], attention_total: 0 },
      }),
      'overview',
    )
    expect(wrapper.text()).toContain('Everything with a health check is up.')
    wrapper.unmount()
  })
})
