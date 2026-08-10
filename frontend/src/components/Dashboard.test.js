/**
 * The Command Center — v1 §8, §7d.
 *
 * Two things are worth asserting from this side, and neither is "the numbers
 * render":
 *
 * 1. **The panel takes its data from one endpoint.** §8 warns that a panel
 *    fetching for itself gets rebuilt, so the test that matters is that a
 *    payload without a panel produces a page without it — the frontend never
 *    fills a gap from somewhere else.
 * 2. **The drawer's actions are a list** (§7d, §12). Proven from both ends: a
 *    service has one, a node has none, and the empty case still renders.
 */

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api.js'
import { state } from '../store.js'
import Dashboard from './Dashboard.vue'

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

function payload(overrides = {}) {
  return {
    landing_page: 'dashboard',
    panels: ['status_summary', 'proxmox_cluster', 'proxmox_nodes'],
    status_summary: SUMMARY,
    proxmox: {
      available: true,
      detail: '',
      polled_at: new Date().toISOString(),
      stale: false,
      cluster: CLUSTER,
      nodes: NODES,
    },
    ...overrides,
  }
}

async function open(body = payload()) {
  vi.spyOn(api, 'getPanels').mockResolvedValue(body)
  const wrapper = mount(Dashboard)
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  vi.restoreAllMocks()
  state.services = []
})

describe('the panel endpoint is the only source', () => {
  it('reads the dashboard with a single call', async () => {
    const wrapper = await open()
    expect(api.getPanels).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('renders no Proxmox panel when the payload has none', async () => {
    // §7d: no token -> no panel. Nothing on the page may fill that gap.
    const wrapper = await open(payload({ proxmox: null, panels: ['status_summary'] }))
    expect(wrapper.text()).not.toContain('Cluster health')
    expect(wrapper.text()).not.toContain('Nodes')
    expect(wrapper.text()).toContain('Status Summary')
    wrapper.unmount()
  })

  it('renders no status panel when it is switched off', async () => {
    const wrapper = await open(
      payload({ status_summary: null, panels: ['proxmox_cluster', 'proxmox_nodes'] }),
    )
    expect(wrapper.text()).not.toContain('Status Summary')
    expect(wrapper.text()).toContain('Cluster health')
    wrapper.unmount()
  })

  it('renders no nodes table when that panel alone is off', async () => {
    const body = payload()
    body.proxmox.nodes = null
    const wrapper = await open(body)
    expect(wrapper.text()).toContain('Cluster health')
    expect(wrapper.find('.nodes-table').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('the staleness affordance (audit informational)', () => {
  // The page's stated purpose is to sit open. A quiet refresh failure used to
  // set no state at all, so every number silently stopped meaning "now" —
  // the catalog says "Not refreshing" for the same condition, and now so
  // does this screen.
  it('says the dashboard has stopped refreshing, without blanking it', async () => {
    const wrapper = await open()
    api.getPanels.mockRejectedValue(new Error('unreachable'))

    document.dispatchEvent(new Event('visibilitychange'))
    await new Promise((resolve) => setTimeout(resolve, 0))
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('Not refreshing — server unreachable')
    // The last good payload keeps rendering; no error notice replaces it.
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

describe('the summary row', () => {
  it('states quorum and HA, not just a colour', async () => {
    const wrapper = await open()
    expect(wrapper.text()).toContain('Quorum')
    expect(wrapper.text()).toContain('Not configured')
    wrapper.unmount()
  })

  it('reads Degraded while a node is offline', async () => {
    const wrapper = await open()
    expect(wrapper.text()).toContain('Degraded')
    wrapper.unmount()
  })

  it('says Healthy on a standalone host, which has no quorum to lose', async () => {
    const body = payload()
    body.proxmox.cluster = {
      ...CLUSTER,
      quorate: null,
      nodes_online: 1,
      nodes_total: 1,
    }
    const wrapper = await open(body)
    expect(wrapper.text()).toContain('Healthy')
    expect(wrapper.text()).toContain('Single host')
    wrapper.unmount()
  })
})

describe('the nodes table', () => {
  it('has exactly the columns §7d names, in order', async () => {
    const wrapper = await open()
    const headers = wrapper
      .findAll('.node-row.head .col-head')
      .map((cell) => cell.text())
    expect(headers).toEqual([
      'Node',
      'Status',
      'Uptime',
      'CPU',
      'RAM',
      'Load avg',
      'Storage',
      'Version',
    ])
    wrapper.unmount()
  })

  it('still lists a node whose per-node read failed', async () => {
    const wrapper = await open()
    const text = wrapper.find('.nodes-table').text()
    expect(text).toContain('pve-b')
    expect(text).toContain('Offline')
    wrapper.unmount()
  })
})

describe('the detail drawer', () => {
  it('opens from a node row with that node’s numbers', async () => {
    const wrapper = await open()
    await wrapper.findAll('.node-row.body')[0].trigger('click')
    const drawer = wrapper.find('.drawer')
    expect(drawer.exists()).toBe(true)
    expect(drawer.text()).toContain('pve-d')
    expect(drawer.text()).toContain('9.1.1')
    expect(drawer.text()).toContain('7d 01:25')
    wrapper.unmount()
  })

  it('offers an Open web UI action pointing at that node', async () => {
    // Validation finding 9. The action list was empty for a node; the operator
    // asked for a link to the node's own Proxmox console.
    const wrapper = await open()
    await wrapper.findAll('.node-row.body')[0].trigger('click')
    const actions = wrapper.findAll('.drawer-action')
    expect(actions).toHaveLength(1)
    expect(actions[0].text()).toContain('Open web UI')
    expect(actions[0].attributes('href')).toBe('https://192.0.2.11:8006')
    wrapper.unmount()
  })

  it('renders the actions list empty rather than hiding it', async () => {
    // §12's line is that actions are a LIST, and the empty case still has to
    // render — a node whose address the provider could not read gets no action
    // rather than a dead link. That is what proves it is data, not fixed rows.
    const body = payload()
    body.proxmox.nodes = [{ ...NODES[0], ip: '' }]
    const wrapper = await open(body)
    await wrapper.findAll('.node-row.body')[0].trigger('click')
    expect(wrapper.find('.drawer').text()).toContain('No actions')
    expect(wrapper.findAll('.drawer-action')).toHaveLength(0)
    wrapper.unmount()
  })

  it('gives a service the one action it has, from the same list', async () => {
    state.services = [
      {
        slug: 'jellyfin',
        name: 'Jellyfin',
        launch_url: 'https://jellyfin.lab.local',
        host_label: 'pve-d · CT 105',
        category: 'media',
      },
    ]
    const wrapper = await open()
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
    // executes `javascript:` on click in every browser. The value is still
    // shown, because an operator needs to see what is wrong with it.
    state.services = [
      {
        slug: 'jellyfin',
        name: 'Jellyfin',
        launch_url: 'javascript:alert(document.cookie)',
      },
    ]
    const wrapper = await open()
    await wrapper.findAll('.dash-list-row')[0].trigger('click')
    const drawer = wrapper.find('.drawer')
    expect(drawer.text()).toContain('javascript:alert(document.cookie)')
    expect(drawer.findAll('a')).toHaveLength(0)
    expect(drawer.findAll('.drawer-action')).toHaveLength(0)
    wrapper.unmount()
  })

  it('omits rows it cannot fill instead of printing an em dash for each', async () => {
    // A service with no link has no version and no uptime — those belong to a
    // node. Showing "—" for every service is noise pretending to be data.
    state.services = [
      { slug: 'jellyfin', name: 'Jellyfin', launch_url: 'https://jellyfin.lab.local' },
    ]
    const wrapper = await open()
    await wrapper.findAll('.dash-list-row')[0].trigger('click')
    const keys = wrapper.findAll('.drawer-key').map((cell) => cell.text())
    expect(keys).toContain('URL')
    expect(keys).not.toContain('Version')
    expect(keys).not.toContain('Uptime')
    wrapper.unmount()
  })
})

describe('degraded states', () => {
  it('shows the reason a cluster is unavailable', async () => {
    const wrapper = await open(
      payload({
        proxmox: {
          available: false,
          detail: 'Proxmox polling is switched off in Settings.',
          polled_at: null,
          stale: false,
          cluster: null,
          nodes: null,
        },
      }),
    )
    expect(wrapper.text()).toContain('Proxmox polling is switched off in Settings.')
    expect(wrapper.find('.nodes-table').exists()).toBe(false)
    wrapper.unmount()
  })

  it('marks a snapshot that stopped refreshing', async () => {
    const body = payload()
    body.proxmox.stale = true
    const wrapper = await open(body)
    expect(wrapper.text()).toContain('Not refreshing')
    wrapper.unmount()
  })

  it('says so when nothing needs attention', async () => {
    const wrapper = await open(
      payload({
        status_summary: { ...SUMMARY, down: 0, stale: 0, attention: [], attention_total: 0 },
      }),
    )
    expect(wrapper.text()).toContain('Everything with a health check is up.')
    wrapper.unmount()
  })
})
