/**
 * Which screen the app opens on — v1 §8.
 *
 * Two behaviours, both of which used to be impossible to have:
 *
 * - **`/` resolves through `ui.landing_page`.** §8 makes the landing page a
 *   server-side setting precisely so the dashboard has to earn the default
 *   rather than be handed it. A settings screen that saves the value while
 *   nothing reads it is standing rule 2's failure — green on both sides,
 *   inert in the middle.
 * - **A reload keeps your place.** `/settings` and `/services/{slug}` used to
 *   drop back to the catalog on refresh (`BACKLOG.3`), so a bookmark or a link
 *   to a service could not exist.
 */

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App.vue'
import { api } from './api.js'
import { resolve, route } from './router.js'
import { state } from './store.js'

const SERVICE = {
  slug: 'jellyfin',
  name: 'Jellyfin',
  launch_url: 'https://jellyfin.lab.local',
  status: 'up',
  tags: [],
  health_enabled: true,
}

function stubApi({ landingPage = 'catalog', services = [SERVICE], workers, views = [] } = {}) {
  vi.spyOn(api, 'listServices').mockResolvedValue(services)
  vi.spyOn(api, 'listViews').mockResolvedValue(views)
  vi.spyOn(api, 'getHealthz').mockResolvedValue({
    ok: true,
    checked_at: new Date().toISOString(),
    workers: workers ?? [
      { name: 'health', state: 'ok', stale: false, last_beat_at: '2026-08-07T10:00:00Z' },
      { name: 'proxmox', state: 'ok', stale: false, last_beat_at: '2026-08-07T10:00:00Z' },
    ],
  })
  vi.spyOn(api, 'getPublicSettings').mockResolvedValue({
    auth_mode: 'full',
    values: { 'ui.landing_page': landingPage },
  })
  vi.spyOn(api, 'getPanels').mockResolvedValue({
    landing_page: landingPage,
    panels: ['status_summary'],
    status_summary: {
      total: 1,
      up: 1,
      down: 0,
      stale: 0,
      unknown: 0,
      off: 0,
      attention: [],
      attention_total: 0,
    },
    proxmox: null,
  })
}

async function openAt(path, options) {
  window.history.replaceState({}, '', path)
  route.value = resolve(path)
  stubApi(options)
  const wrapper = mount(App)
  // Two ticks: the catalog load and the public-settings read that `/` waits on.
  await new Promise((r) => setTimeout(r, 0))
  await new Promise((r) => setTimeout(r, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('the landing page', () => {
  it('opens the catalog at / by default', async () => {
    const wrapper = await openAt('/')
    expect(window.location.pathname).toBe('/catalog')
    expect(wrapper.text()).toContain('Catalog')
    wrapper.unmount()
  })

  it('opens the Command Center at / when that is the setting', async () => {
    const wrapper = await openAt('/', { landingPage: 'dashboard' })
    expect(window.location.pathname).toBe('/dashboard')
    expect(wrapper.text()).toContain('Command Center')
    wrapper.unmount()
  })

  it('leaves /catalog alone even when the dashboard is the landing page', async () => {
    // §8: "the Service Catalog stays reachable at /catalog regardless."
    const wrapper = await openAt('/catalog', { landingPage: 'dashboard' })
    expect(window.location.pathname).toBe('/catalog')
    wrapper.unmount()
  })
})

describe('a cold load of a deep link', () => {
  it('opens the settings screen at /settings', async () => {
    const wrapper = await openAt('/settings')
    expect(wrapper.text()).toContain('Settings')
    wrapper.unmount()
  })

  it('opens a service record at /services/{slug}', async () => {
    const wrapper = await openAt('/services/jellyfin')
    expect(wrapper.text()).toContain('Jellyfin')
    wrapper.unmount()
  })

  it('says so when the slug is not in the catalog', async () => {
    // Rendering the catalog instead would read as a working link to the wrong
    // page, which is the failure nobody reports.
    const wrapper = await openAt('/services/deleted-service')
    expect(wrapper.text()).toContain('No service with that address')
    wrapper.unmount()
  })

  it('says so for a path the app does not own', async () => {
    const wrapper = await openAt('/nope')
    expect(wrapper.text()).toContain('Page not found')
    wrapper.unmount()
  })
})

describe('navigation', () => {
  it('puts the service record in the URL when a row is opened', async () => {
    const wrapper = await openAt('/catalog')
    await wrapper.find('.svc').trigger('click')
    await wrapper.vm.$nextTick()
    expect(window.location.pathname).toBe('/services/jellyfin')
    wrapper.unmount()
  })
})

describe('the sidebar', () => {
  // Findings 2 and 4 from the V1.5 validation pass: navigation belongs in the
  // rail, Settings belongs at the foot, and both workers report there.
  it('offers Command Center and Catalog as the first two nav items', async () => {
    const wrapper = await openAt('/catalog')
    const labels = wrapper.findAll('.side-nav .nav-label').map((el) => el.text())
    expect(labels).toEqual(['Command Center', 'Catalog'])
    wrapper.unmount()
  })

  it('marks the current screen active in the rail', async () => {
    const wrapper = await openAt('/dashboard')
    const active = wrapper.find('.side-nav .nav-item.active .nav-label')
    expect(active.text()).toBe('Command Center')
    wrapper.unmount()
  })

  it('styles nav as buttons, distinct from the filter rows below', async () => {
    // Reported 2026-08-07: the two nav items reused `.side-item` and so read as
    // more links in the same list as saved views and categories. They are
    // destinations, not filters, and now say so.
    const wrapper = await openAt('/catalog')
    expect(wrapper.findAll('.nav-item')).toHaveLength(2)
    // No nav item may fall back to the filter-row class.
    expect(wrapper.findAll('.side-nav .side-item')).toHaveLength(0)
    wrapper.unmount()
  })

  it('rules off only the foot, not the nav', async () => {
    // A rule under the nav was tried and dropped on 2026-08-07: once the nav
    // items became buttons they separated themselves, and the extra line was
    // noise. The one above Settings stays, because the worker rows beside it
    // are not navigation.
    const wrapper = await openAt('/catalog')
    expect(wrapper.findAll('.side-rule')).toHaveLength(1)
    expect(wrapper.find('.side-foot .side-rule').exists()).toBe(true)
    wrapper.unmount()
  })

  it('carries no decorative icon block on the nav items', async () => {
    // Dropped as noise: the active dot alone says which screen you are on.
    const wrapper = await openAt('/catalog')
    expect(wrapper.findAll('.nav-icon')).toHaveLength(0)
    expect(wrapper.findAll('.nav-dot')).toHaveLength(2)
    wrapper.unmount()
  })

  it('puts Settings in the foot, not the top bar', async () => {
    const wrapper = await openAt('/catalog')
    expect(wrapper.find('.side-foot').text()).toContain('Settings')
    expect(wrapper.find('.topbar').text()).not.toContain('Settings')
    wrapper.unmount()
  })

  it('reports BOTH workers from the heartbeat', async () => {
    // The old footer inferred liveness from the count of stale services, which
    // is no signal at all for the Proxmox worker — it has no services to make
    // stale, so it could die unnoticed.
    const wrapper = await openAt('/catalog')
    const foot = wrapper.find('.side-foot').text()
    expect(foot).toContain('Health worker reporting')
    expect(foot).toContain('Proxmox worker reporting')
    wrapper.unmount()
  })

  it('says which worker stopped, rather than going quiet', async () => {
    const wrapper = await openAt('/catalog', {
      workers: [
        { name: 'health', state: 'ok', stale: false, last_beat_at: '2026-08-07T10:00:00Z' },
        { name: 'proxmox', state: 'stale', stale: true, last_beat_at: '2026-08-07T09:00:00Z' },
      ],
    })
    const foot = wrapper.find('.side-foot').text()
    expect(foot).toContain('Health worker reporting')
    expect(foot).toContain('Proxmox worker not reporting')
    wrapper.unmount()
  })

  it('says so when no worker has ever reported', async () => {
    const wrapper = await openAt('/catalog', { workers: [] })
    expect(wrapper.find('.side-foot').text()).toContain('No worker has reported yet')
    wrapper.unmount()
  })

  it('moved Export JSON and the API docs off the top bar', async () => {
    const wrapper = await openAt('/catalog')
    const topbar = wrapper.find('.topbar').text()
    expect(topbar).not.toContain('Export JSON')
    expect(topbar).not.toContain('API')
    wrapper.unmount()
  })
})

describe('the brand button', () => {
  it('returns to the catalog when that is the landing page', async () => {
    const wrapper = await openAt('/settings')
    await wrapper.find('.brand').trigger('click')
    await wrapper.vm.$nextTick()
    expect(window.location.pathname).toBe('/catalog')
    wrapper.unmount()
  })

  it('returns to the Command Center when THAT is the landing page', async () => {
    // Finding 6: it was hardcoded to the catalog, so an operator who had chosen
    // the dashboard as home could not get back to it from the brand.
    const wrapper = await openAt('/settings', { landingPage: 'dashboard' })
    await wrapper.find('.brand').trigger('click')
    await wrapper.vm.$nextTick()
    expect(window.location.pathname).toBe('/dashboard')
    wrapper.unmount()
  })
})

describe('category filtering from the sidebar', () => {
  const TWO = [
    { ...SERVICE, slug: 'ollama', name: 'Ollama', category: 'ai' },
    { ...SERVICE, slug: 'proxmox', name: 'Proxmox', category: 'infrastructure' },
  ]

  it('replaces the active category instead of accumulating', async () => {
    // Finding 3: clicking AI then Infrastructure used to show both.
    const wrapper = await openAt('/catalog', { services: TWO })
    const category = (name) =>
      wrapper
        .findAll('.side-item')
        .find((el) => el.text().startsWith(name))

    await category('ai').trigger('click')
    await wrapper.vm.$nextTick()
    expect(state.query).toBe('cat:ai')

    await category('infrastructure').trigger('click')
    await wrapper.vm.$nextTick()
    expect(state.query).toBe('cat:infrastructure')
    wrapper.unmount()
  })

  it('clears when the active category is clicked again', async () => {
    const wrapper = await openAt('/catalog', { services: TWO })
    const ai = wrapper.findAll('.side-item').find((el) => el.text().startsWith('ai'))
    await ai.trigger('click')
    await wrapper.vm.$nextTick()
    await ai.trigger('click')
    await wrapper.vm.$nextTick()
    expect(state.query).toBe('')
    wrapper.unmount()
  })
})

describe('saved views can be renamed', () => {
  // Reported 2026-08-07: categories had a rename pencil and saved views did
  // not, so a typo in a view name was unfixable except by deleting it and
  // rebuilding the query.
  const VIEWS = [{ id: 'v1', name: 'Unhealthy', query: 'state:!up', position: 0 }]

  it('offers a pencil on each saved view, like categories have', async () => {
    const wrapper = await openAt('/catalog', { views: VIEWS })
    const pencil = wrapper
      .findAll('.side-edit')
      .find((b) => b.attributes('title')?.includes('Unhealthy'))
    expect(pencil).toBeTruthy()
    wrapper.unmount()
  })

  it('opens the editor for the view, not for a category', async () => {
    const wrapper = await openAt('/catalog', { views: VIEWS })
    const pencil = wrapper
      .findAll('.side-edit')
      .find((b) => b.attributes('title')?.includes('Unhealthy'))
    await pencil.trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('Edit saved view')
    // The view's own hint, not the category one about N services.
    expect(wrapper.text()).toContain('Renaming changes only its label')
    wrapper.unmount()
  })

  it('does not run the view query when the pencil is clicked', async () => {
    // The pencil sits inside the row that applies the filter, so without
    // `@click.stop` renaming would also filter the catalog.
    const wrapper = await openAt('/catalog', { views: VIEWS })
    state.query = ''
    const pencil = wrapper
      .findAll('.side-edit')
      .find((b) => b.attributes('title')?.includes('Unhealthy'))
    await pencil.trigger('click')
    expect(state.query).toBe('')
    wrapper.unmount()
  })
})

describe('"back" means the catalog, not the landing page (validation 2026-08-07, findings 1 & 2)', () => {
  // With the dashboard as the landing page, @back="goHome" sent both the
  // settings screen's "Back to the catalog" button and the post-delete return
  // to the Command Center. The button's label is the contract: it goes to
  // /catalog whatever the landing page says.
  it('settings back button lands on /catalog even when home is the dashboard', async () => {
    vi.spyOn(api, 'getSettings').mockResolvedValue({
      auth_mode: 'full',
      sections: [],
      environment: [],
      providers: [],
      settings: [],
    })
    const wrapper = await openAt('/settings', { landingPage: 'dashboard' })
    const back = wrapper
      .findAll('button')
      .find((b) => b.text().includes('Back to the catalog'))
    await back.trigger('click')
    expect(window.location.pathname).toBe('/catalog')
    wrapper.unmount()
  })

  it('a service record back button lands on /catalog too — the delete flow emits the same event', async () => {
    const wrapper = await openAt('/services/jellyfin', { landingPage: 'dashboard' })
    const back = wrapper
      .findAll('button')
      .find((b) => b.text().includes('Catalog') && b.classes().includes('link-btn'))
    await back.trigger('click')
    expect(window.location.pathname).toBe('/catalog')
    wrapper.unmount()
  })
})

describe('write-path error handling (audit S11)', () => {
  // `withWriteGate` intercepts only the needs-auth case; a 422, 500 or network
  // failure is rethrown to the caller. These three used to have no catch, so
  // the promise rejected to the console, the modal stayed open and the button
  // read as dead. Every other component already catches and displays.
  async function flush(wrapper) {
    await new Promise((r) => setTimeout(r, 0))
    await wrapper.vm.$nextTick()
  }

  it('shows a failed view save in the modal instead of dying silently', async () => {
    const wrapper = await openAt('/catalog')
    vi.spyOn(api, 'createView').mockRejectedValue(new Error('The server is restarting.'))

    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Save view')
      .trigger('click')
    await wrapper.vm.$nextTick()
    await wrapper.find('.modal input').setValue('My view')
    await wrapper.find('.modal .btn-primary').trigger('click')
    await flush(wrapper)

    expect(wrapper.find('.modal').exists()).toBe(true)
    expect(wrapper.find('.modal .notice.err').text()).toContain('The server is restarting.')
    wrapper.unmount()
  })

  it('shows a failed bulk tag in its modal and keeps the selection', async () => {
    const wrapper = await openAt('/catalog')
    vi.spyOn(api, 'patchService').mockRejectedValue(new Error('bad gateway'))
    state.selected = new Set(['jellyfin'])
    await wrapper.vm.$nextTick()

    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Add tags'))
      .trigger('click')
    await wrapper.vm.$nextTick()
    const input = wrapper.find('.modal input')
    await input.setValue('newtag')
    await input.trigger('keydown', { key: 'Enter' })
    await wrapper.find('.modal .btn-primary').trigger('click')
    await flush(wrapper)

    expect(wrapper.find('.modal .notice.err').text()).toContain('bad gateway')
    expect(state.selected.has('jellyfin')).toBe(true)
    wrapper.unmount()
  })

  it('shows a failed bulk category set in its modal', async () => {
    const wrapper = await openAt('/catalog')
    vi.spyOn(api, 'patchService').mockRejectedValue(new Error('bad gateway'))
    state.selected = new Set(['jellyfin'])
    await wrapper.vm.$nextTick()

    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Set category')
      .trigger('click')
    await wrapper.vm.$nextTick()
    await wrapper.find('.modal input').setValue('Media')
    await wrapper.find('.modal .btn-primary').trigger('click')
    await flush(wrapper)

    expect(wrapper.find('.modal').exists()).toBe(true)
    expect(wrapper.find('.modal .notice.err').text()).toContain('bad gateway')
    wrapper.unmount()
  })

  it('clears the error when the modal is reopened', async () => {
    // A failure message belongs to one showing of the modal; replaying the
    // last attempt's error over a fresh form misreports the current state.
    const wrapper = await openAt('/catalog')
    vi.spyOn(api, 'createView').mockRejectedValue(new Error('boom'))

    const openButton = wrapper.findAll('button').find((b) => b.text() === 'Save view')
    await openButton.trigger('click')
    await wrapper.vm.$nextTick()
    await wrapper.find('.modal input').setValue('My view')
    await wrapper.find('.modal .btn-primary').trigger('click')
    await flush(wrapper)
    expect(wrapper.find('.modal .notice.err').exists()).toBe(true)

    await wrapper.find('.modal .btn-ghost').trigger('click')
    await wrapper.vm.$nextTick()
    await openButton.trigger('click')
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.modal .notice.err').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('the nav drawer (responsive shell)', () => {
  // Below the desktop breakpoint the sidebar is an overlay drawer opened by
  // the topbar's ☰. jsdom has no layout, so these exercise the width-agnostic
  // state machine; the presentation itself is CSS and is verified on devices.
  const VIEWS = [{ id: 'v1', name: 'Unhealthy', query: 'state:!up', position: 0 }]

  it('renders the ☰ in the topbar and starts closed', async () => {
    const wrapper = await openAt('/catalog')
    const toggle = wrapper.find('.topbar .nav-toggle')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.app').classes()).not.toContain('nav-open')
    expect(wrapper.find('.nav-backdrop').exists()).toBe(false)
    wrapper.unmount()
  })

  it('opens on ☰ — class, backdrop and aria-expanded all agree', async () => {
    const wrapper = await openAt('/catalog')
    await wrapper.find('.nav-toggle').trigger('click')
    expect(wrapper.find('.app').classes()).toContain('nav-open')
    expect(wrapper.find('.nav-backdrop').exists()).toBe(true)
    expect(wrapper.find('.nav-toggle').attributes('aria-expanded')).toBe('true')
    wrapper.unmount()
  })

  it('closes on a backdrop tap', async () => {
    const wrapper = await openAt('/catalog')
    await wrapper.find('.nav-toggle').trigger('click')
    await wrapper.find('.nav-backdrop').trigger('click')
    expect(wrapper.find('.app').classes()).not.toContain('nav-open')
    expect(wrapper.find('.nav-backdrop').exists()).toBe(false)
    wrapper.unmount()
  })

  it('closes on Escape', async () => {
    const wrapper = await openAt('/catalog')
    await wrapper.find('.nav-toggle').trigger('click')
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.app').classes()).not.toContain('nav-open')
    wrapper.unmount()
  })

  it('closes when navigating — to a different screen', async () => {
    const wrapper = await openAt('/catalog')
    await wrapper.find('.nav-toggle').trigger('click')
    const dashboard = wrapper
      .findAll('.side-nav .nav-item')
      .find((el) => el.text().includes('Command Center'))
    await dashboard.trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.app').classes()).not.toContain('nav-open')
    wrapper.unmount()
  })

  it('closes when navigating to the screen you are already on', async () => {
    // Works because navigate() reassigns route.value with a fresh object even
    // for an unchanged path (router.js), so the route watcher fires. This test
    // pins that dependency: a same-path early return in navigate() breaks the
    // drawer here first.
    const wrapper = await openAt('/catalog')
    await wrapper.find('.nav-toggle').trigger('click')
    const catalog = wrapper
      .findAll('.side-nav .nav-item')
      .find((el) => el.text().includes('Catalog'))
    await catalog.trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.app').classes()).not.toContain('nav-open')
    wrapper.unmount()
  })

  it('closes the moment a modal opens from inside it', async () => {
    // This is what keeps Escape unambiguous: BaseModal's stack owns Escape
    // while any modal is up, so the drawer must never still be open behind one.
    const wrapper = await openAt('/catalog', { views: VIEWS })
    await wrapper.find('.nav-toggle').trigger('click')
    const pencil = wrapper
      .findAll('.side-edit')
      .find((b) => b.attributes('title')?.includes('Unhealthy'))
    await pencil.trigger('click')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.modal').exists()).toBe(true)
    expect(wrapper.find('.app').classes()).not.toContain('nav-open')
    wrapper.unmount()
  })
})
