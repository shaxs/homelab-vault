/**
 * The catalog table — spec §5, Layout 2B.
 *
 * **This file exists because this component is the project's recorded blind
 * spot.** V1.4 shipped derived location with the producer and the service record
 * both well tested, and missed that `CatalogTable` renders `host_label` — so the
 * Host column showed "—" for every service and the operator found it in the
 * first minute of real use. The mistake-note's rule is that "producer tested, one
 * consumer tested" is incomplete by default. The list view had no tests at all
 * until now.
 *
 * The assertions here are about the *shape of the row* — which columns exist, in
 * what order, and what each cell emits — because that is the seam a change to
 * the table silently breaks.
 */

import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api.js'
import { state } from '../store.js'
import CatalogTable from './CatalogTable.vue'

const SERVICE = {
  slug: 'jellyfin',
  name: 'Jellyfin',
  category: 'Media',
  tags: ['video', 'transcode'],
  status: 'up',
  host_label: 'pve-c · CT 117',
  launch_url: 'https://jellyfin.lab.local',
  health_enabled: true,
  icon_url: null,
}

function table(rows = [SERVICE]) {
  return mount(CatalogTable, { props: { rows, total: rows.length } })
}

describe('columns', () => {
  it('has Category between Service and Status', () => {
    // v0 spec §5 listed Category as its own column; it spent v0 and v1 as a
    // subline under the service name because the mockup drew it that way, and
    // the two lines read as one crowded block. Restored 2026-08-07.
    const heads = table().findAll('.col-head').map((h) => h.text())
    expect(heads).toEqual([
      'Service',
      'Category',
      'Status',
      'Location',
      'Tags',
      'Launch',
    ])
  })

  it('no longer stacks the category under the service name', () => {
    const wrapper = table()
    expect(wrapper.find('.svc-cat').exists()).toBe(false)
    expect(wrapper.find('.svc').text()).not.toContain('Media')
    wrapper.unmount()
  })

  it('renders the category in its own cell', () => {
    const wrapper = table()
    expect(wrapper.find('.cat-cell').text()).toBe('Media')
    wrapper.unmount()
  })

  it('shows an em dash, not a chip, when a service has no category', () => {
    const wrapper = table([{ ...SERVICE, category: null }])
    expect(wrapper.find('.cat-chip').exists()).toBe(false)
    expect(wrapper.find('.cat-empty').text()).toBe('—')
    wrapper.unmount()
  })
})

describe('the category chip filters', () => {
  it('emits a single-select term, not an accumulating one', () => {
    // A service has exactly one category, so clicking a second must replace the
    // first. Tags accumulate; category does not. The two events are different on
    // purpose — see `selectOne` vs `addTerm` in App.vue.
    const wrapper = table()
    wrapper.find('.cat-chip').trigger('click')
    expect(wrapper.emitted('select-term')).toEqual([['cat:Media']])
    expect(wrapper.emitted('append-term')).toBeUndefined()
    wrapper.unmount()
  })

  it('does not also open the record', () => {
    // The chip sits inside a row whose other cells open the service, so without
    // `@click.stop` filtering would navigate away from the result.
    const wrapper = table()
    wrapper.find('.cat-chip').trigger('click')
    expect(wrapper.emitted('open')).toBeUndefined()
    wrapper.unmount()
  })

  it('still lets tags accumulate', () => {
    const wrapper = table()
    wrapper.findAll('.tag')[0].trigger('click')
    expect(wrapper.emitted('append-term')).toEqual([['tag:video']])
    expect(wrapper.emitted('select-term')).toBeUndefined()
    wrapper.unmount()
  })
})

describe('the columns V1.4 broke once already', () => {
  it('falls back to the derived location when nothing is pinned', () => {
    // The exact regression from the mistake-note: unpinned is the default, so a
    // Host column reading only `host_label` shows "—" for every service.
    const wrapper = table([
      { ...SERVICE, host_label: null, location_derived: 'pve-c · CT 117' },
    ])
    expect(wrapper.find('.host').text()).toBe('pve-c · CT 117')
    wrapper.unmount()
  })

  it('prefers a pinned location over the derived one', () => {
    const wrapper = table([
      { ...SERVICE, host_label: 'Pinned', location_derived: 'pve-c · CT 117' },
    ])
    expect(wrapper.find('.host').text()).toBe('Pinned')
    wrapper.unmount()
  })

  it('shows an em dash when there is neither', () => {
    const wrapper = table([
      { ...SERVICE, host_label: null, location_derived: null },
    ])
    expect(wrapper.find('.host').text()).toBe('—')
    wrapper.unmount()
  })
})

describe('catalog density (BACKLOG.5, ui.catalog.density)', () => {
  // Standing rule 2, client half: the setting must reach THIS consumer. The
  // backend half proves the key is served publicly; this proves the table
  // actually changes shape when it arrives.
  afterEach(() => {
    state.publicSettings = {}
  })

  it('renders compact by default — existing installs look identical', () => {
    state.publicSettings = {}
    const wrapper = table()
    expect(wrapper.find('.table').classes()).not.toContain('density-comfortable')
    wrapper.unmount()
  })

  it('applies the comfortable class when the public setting says so', () => {
    state.publicSettings = { 'ui.catalog.density': 'comfortable' }
    const wrapper = table()
    expect(wrapper.find('.table').classes()).toContain('density-comfortable')
    wrapper.unmount()
  })

  it('treats an unknown value as compact rather than guessing', () => {
    state.publicSettings = { 'ui.catalog.density': 'gigantic' }
    const wrapper = table()
    expect(wrapper.find('.table').classes()).not.toContain('density-comfortable')
    wrapper.unmount()
  })
})

describe('a stale derived location (audit S8 — the UI half)', () => {
  // The API has served `location_stale` since Window 2: true once the provider
  // has missed the linked guest for three consecutive successful polls, while
  // the location itself keeps being served (stale-and-said beats silently
  // current-looking). This is the screen that says it.
  it('marks a stale derived location', () => {
    const wrapper = table([
      {
        ...SERVICE,
        host_label: null,
        location_derived: 'pve-c · CT 117',
        location_stale: true,
      },
    ])
    expect(wrapper.find('.host .loc-tag.warn').text()).toBe('stale')
    wrapper.unmount()
  })

  it('leaves a live derived location unmarked', () => {
    const wrapper = table([
      {
        ...SERVICE,
        host_label: null,
        location_derived: 'pve-c · CT 117',
        location_stale: false,
      },
    ])
    expect(wrapper.find('.host .loc-tag').exists()).toBe(false)
    wrapper.unmount()
  })

  it('never marks a pinned value — the pin belongs to the operator, not the provider', () => {
    // Pinned wins on display (§4a); staleness of the hidden derived value says
    // nothing about the value actually on screen.
    const wrapper = table([{ ...SERVICE, location_derived: 'pve-c · CT 117', location_stale: true }])
    expect(wrapper.find('.host .loc-tag').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('recheck failures (audit S11)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    state.error = ''
  })

  it('surfaces a failed recheck instead of dying silently', async () => {
    // try…finally with no catch meant a 500 or a network failure rejected to
    // the console: the spinner just stopped and nothing said why.
    vi.spyOn(api, 'recheck').mockRejectedValue(new Error('gateway timeout'))
    state.error = ''
    const wrapper = table()

    await wrapper.find('.status-cell').trigger('click')
    await new Promise((r) => setTimeout(r, 0))
    await wrapper.vm.$nextTick()

    expect(state.error).toContain('gateway timeout')
    // The row's own spinner is released either way.
    expect(wrapper.find('.status-spin').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('launching vs editing (2026-10-28)', () => {
  // The icon and name used to open the record; operators kept clicking them
  // expecting the service. They launch now, and the record moved to Edit.
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('launches the service when the icon or name is clicked', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = table()
    wrapper.find('.svc').trigger('click')
    expect(open).toHaveBeenCalledWith('https://jellyfin.lab.local', '_blank', 'noopener')
    expect(wrapper.emitted('open')).toBeUndefined()
    expect(wrapper.emitted('edit')).toBeUndefined()
    wrapper.unmount()
  })

  it('opens the editor instead when the URL cannot be launched', () => {
    // `launch()` ignores a URL `safeUrl` rejects, so a launch here would be a
    // click that does nothing. The URL is the thing to fix, so go there.
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const broken = { ...SERVICE, launch_url: 'javascript:alert(1)' }
    const wrapper = table([broken])
    wrapper.find('.svc').trigger('click')
    expect(open).not.toHaveBeenCalled()
    expect(wrapper.emitted('edit')).toEqual([[broken]])
    wrapper.unmount()
  })

  it('opens the editor from the Edit button, without launching', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = table()
    wrapper.find('.launch .edit-btn').trigger('click')
    expect(wrapper.emitted('edit')).toEqual([[SERVICE]])
    expect(wrapper.emitted('open')).toBeUndefined()
    expect(open).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('puts Edit before Open in the launch cell', () => {
    const labels = table().findAll('.launch button').map((b) => b.text())
    expect(labels).toEqual(['Edit', 'Open ↗'])
  })

  it('still launches from Open', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = table()
    wrapper.find('.launch-btn').trigger('click')
    expect(open).toHaveBeenCalledWith('https://jellyfin.lab.local', '_blank', 'noopener')
    expect(wrapper.emitted('edit')).toBeUndefined()
    wrapper.unmount()
  })

  it('still opens the record to view from the Location cell', () => {
    // Only the icon and name changed meaning; the rest of the row did not.
    const wrapper = table()
    wrapper.find('.host').trigger('click')
    expect(wrapper.emitted('open')).toEqual([[SERVICE]])
    expect(wrapper.emitted('edit')).toBeUndefined()
    wrapper.unmount()
  })
})
