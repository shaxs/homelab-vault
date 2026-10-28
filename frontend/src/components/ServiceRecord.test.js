/**
 * The service record's edit form — v1 §4, §6a.
 *
 * **Why this file exists.** Two V1.3 bugs got past 463 backend and 97 frontend
 * tests and were found by a human on the first click: `notify_enabled` could be
 * set at create but not at update, and there was no way at all to set a
 * per-service poll interval. Both live in the payload this form sends, and
 * nothing anywhere asserted what that payload contains — the backend tests
 * built services with `Service.objects.create()`, and the frontend had no test
 * for this component whatsoever.
 *
 * So these assert the **shape of the save**, which is the seam the two bugs
 * fell through. A field the form never sends is a feature the API can support
 * perfectly and no user can reach.
 */

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api.js'
import * as store from '../store.js'
import ServiceRecord from './ServiceRecord.vue'

const SERVICE = {
  id: '1',
  slug: 'omniroute',
  name: 'OmniRoute',
  launch_url: 'https://omniroute.lab.local',
  launch_url_external: null,
  health_url: null,
  category: null,
  tags: [],
  icon_url: null,
  description: null,
  host_label: null,
  provenance: 'manual',
  created_by: null,
  ignored: false,
  repo_url: null,
  docs_url: null,
  launch_target: 'new_tab',
  health_enabled: true,
  notify_enabled: true,
  health_expect_status: null,
  health_interval_s: null,
  health_timeout_ms: 5000,
  health_fail_threshold: null,
  health_interval_effective_s: 60,
  health_status: 'up',
  status: 'up',
  health_checked_at: '2026-08-06T12:00:00Z',
  health_changed_at: null,
  health_consec_failures: 0,
  health_consec_successes: 3,
  created_at: '2026-08-01T12:00:00Z',
  updated_at: '2026-08-01T12:00:00Z',
}

function record(overrides = {}) {
  return { ...SERVICE, ...overrides }
}

let patched

async function openEditor(service = record()) {
  patched = []
  vi.spyOn(api, 'patchService').mockImplementation(async (slug, body) => {
    patched.push({ slug, body })
    return { ...service, ...body }
  })
  // The write gate is V1.2's; here it should just let the call through.
  vi.spyOn(store, 'withWriteGate').mockImplementation((action) => action())
  vi.spyOn(store, 'upsertLocal').mockImplementation(() => {})

  const wrapper = mount(ServiceRecord, { props: { service } })
  await wrapper.findAll('button').find((b) => b.text() === 'Edit').trigger('click')
  await wrapper.vm.$nextTick()
  return wrapper
}

async function save(wrapper) {
  await wrapper.findAll('button').find((b) => b.text() === 'Save').trigger('click')
  await wrapper.vm.$nextTick()
  await new Promise((resolve) => setTimeout(resolve, 0))
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('the edit form payload', () => {
  it('sends notify_enabled — the field the API rejected the whole save without', async () => {
    const wrapper = await openEditor()
    await save(wrapper)

    expect(patched).toHaveLength(1)
    expect(patched[0].body).toHaveProperty('notify_enabled')
    wrapper.unmount()
  })

  it('turns notifications off without touching health checks', async () => {
    // §4's separation: these are two switches, and one must not move the other.
    const wrapper = await openEditor()
    const boxes = wrapper.findAll('input[type="checkbox"]')
    await boxes[boxes.length - 1].setValue(false)
    await save(wrapper)

    expect(patched[0].body.notify_enabled).toBe(false)
    expect(patched[0].body.health_enabled).toBe(true)
    wrapper.unmount()
  })
})

describe('the per-service interval', () => {
  it('starts empty when the service follows the global setting', async () => {
    // Null is a real value — "follow Settings" — and an <input> turns
    // everything into text, so it has to survive the round trip as empty.
    const wrapper = await openEditor()
    const interval = wrapper.find('input[type="number"]')
    expect(interval.element.value).toBe('')
    wrapper.unmount()
  })

  it('sends a typed interval as a number, pinning the service', async () => {
    const wrapper = await openEditor()
    await wrapper.find('input[type="number"]').setValue('300')
    await save(wrapper)

    expect(patched[0].body.health_interval_s).toBe(300)
    wrapper.unmount()
  })

  it('sends null when the field is cleared, so the service follows the global again', async () => {
    // The half that is easy to get wrong: an empty <input> is "", and "" is not
    // null — it would reach the API as a 422 or, worse, as 0.
    const wrapper = await openEditor(record({ health_interval_s: 300 }))
    const interval = wrapper.find('input[type="number"]')
    expect(interval.element.value).toBe('300')

    await interval.setValue('')
    await save(wrapper)

    expect(patched[0].body.health_interval_s).toBeNull()
    wrapper.unmount()
  })

  it('shows the effective interval as the placeholder, so empty is not a mystery', async () => {
    const wrapper = await openEditor()
    expect(wrapper.find('input[type="number"]').attributes('placeholder')).toContain('60')
    wrapper.unmount()
  })

  it('sends the fail threshold the same way', async () => {
    const wrapper = await openEditor()
    const numbers = wrapper.findAll('input[type="number"]')
    await numbers[1].setValue('5')
    await save(wrapper)

    expect(patched[0].body.health_fail_threshold).toBe(5)
    wrapper.unmount()
  })
})

describe('the read-only health panel', () => {
  it('says whether the interval is pinned or followed', async () => {
    const following = mount(ServiceRecord, { props: { service: record() } })
    expect(following.text()).toContain('follows Settings')
    following.unmount()

    const pinned = mount(ServiceRecord, {
      props: { service: record({ health_interval_s: 300, health_interval_effective_s: 300 }) },
    })
    expect(pinned.text()).toContain('pinned')
    pinned.unmount()
  })

  it('says a silenced service is still checked', async () => {
    const wrapper = mount(ServiceRecord, {
      props: { service: record({ notify_enabled: false }) },
    })
    expect(wrapper.text()).toContain('checked, never announced')
    wrapper.unmount()
  })
})

describe('durations on the health panel', () => {
  it('shows the timeout in seconds, like every other duration beside it', async () => {
    // VALIDATION_V1.3 finding 9: `timeout 3000ms` next to `1000s` reads as the
    // larger of the two while being 300x smaller. Correct, and unreadable.
    const wrapper = mount(ServiceRecord, {
      props: {
        service: record({
          health_interval_s: 1000,
          health_interval_effective_s: 1000,
          health_timeout_ms: 3000,
        }),
      },
    })
    expect(wrapper.text()).toContain('timeout 3s')
    expect(wrapper.text()).not.toContain('3000ms')
    wrapper.unmount()
  })

  it('keeps a fractional timeout readable rather than rounding it away', async () => {
    const wrapper = mount(ServiceRecord, {
      props: { service: record({ health_timeout_ms: 2500 }) },
    })
    expect(wrapper.text()).toContain('timeout 2.5s')
    wrapper.unmount()
  })
})

describe('location: manual vs derived (§4a)', () => {
  function mountRecord(service) {
    patched = []
    vi.spyOn(api, 'patchService').mockImplementation(async (slug, body) => {
      patched.push({ slug, body })
      return { ...service, ...body }
    })
    vi.spyOn(store, 'withWriteGate').mockImplementation((action) => action())
    vi.spyOn(store, 'upsertLocal').mockImplementation(() => {})
    return mount(ServiceRecord, { props: { service } })
  }

  const DERIVED = {
    location_derived: 'pve-c · CT 105',
    location_provider: 'proxmox',
    location_disagrees: false,
  }

  it('shows the derived location when nothing is pinned', () => {
    const wrapper = mountRecord(record(DERIVED))
    expect(wrapper.text()).toContain('pve-c · CT 105')
    expect(wrapper.text()).toContain('from proxmox')
    expect(wrapper.find('.loc-disagree').exists()).toBe(false)
    wrapper.unmount()
  })

  it('marks a stale derived location (audit S8 — the UI half)', () => {
    // The API has served `location_stale` since Window 2: the provider has
    // missed this guest for three consecutive successful polls, and the value
    // is still served. Stale-and-said beats silently current-looking.
    const wrapper = mountRecord(record({ ...DERIVED, location_stale: true }))
    expect(wrapper.find('.loc-tag.warn').text()).toBe('stale')
    wrapper.unmount()
  })

  it('leaves a live derived location unmarked', () => {
    const wrapper = mountRecord(record({ ...DERIVED, location_stale: false }))
    expect(wrapper.find('.loc-tag.warn').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows both values and a badge when they disagree', () => {
    // §4a: "disagreement is shown, not resolved".
    const wrapper = mountRecord(
      record({ ...DERIVED, host_label: 'pve-old · CT 105', location_disagrees: true }),
    )
    expect(wrapper.find('.loc-disagree').exists()).toBe(true)
    expect(wrapper.text()).toContain('pve-old · CT 105')  // pinned, still shown
    expect(wrapper.text()).toContain('pve-c · CT 105')   // derived, beside it
    wrapper.unmount()
  })

  it('shows no badge when a pin agrees with the derived value', () => {
    // Otherwise every correctly-pinned service nags forever.
    const wrapper = mountRecord(
      record({ ...DERIVED, host_label: 'pve-c · CT 105', location_disagrees: false }),
    )
    expect(wrapper.find('.loc-disagree').exists()).toBe(false)
    wrapper.unmount()
  })

  it('unpins by sending host_label: null and nothing else', async () => {
    // The whole affordance, and the reason it needs no new endpoint.
    const wrapper = mountRecord(
      record({ ...DERIVED, host_label: 'pve-old · CT 105', location_disagrees: true }),
    )
    await wrapper.find('.loc-disagree button').trigger('click')
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(patched).toHaveLength(1)
    expect(patched[0].body).toEqual({ host_label: null })
    wrapper.unmount()
  })

  it('offers no "accept" affordance', () => {
    // Accept re-pins, so the pair diverges again on the next migration and the
    // operator accepts forever (§4a, 2026-08-03 correction). The button must
    // not exist — a test that only checks unpin works would pass with both.
    const wrapper = mountRecord(
      record({ ...DERIVED, host_label: 'pve-old · CT 105', location_disagrees: true }),
    )
    const labels = wrapper.findAll('button').map((b) => b.text().toLowerCase())
    expect(labels.some((t) => t.includes('accept'))).toBe(false)
    wrapper.unmount()
  })

  it('states the known limits on the edit form', async () => {
    // §7b: stated rather than discovered. Docker-hosted services are ~half the
    // catalog on a typical cluster, so this is not a footnote.
    const wrapper = await openEditor(record(DERIVED))
    const text = wrapper.text()
    expect(text).toContain('host')
    expect(text).toMatch(/QEMU guest agent/i)
    wrapper.unmount()
  })

  it('a pinned value still saves through the ordinary Location field', async () => {
    // Deliberate re-pinning stays available, just not labelled "accept".
    const wrapper = await openEditor(record(DERIVED))
    const inputs = wrapper.findAll('input')
    const locationInput = inputs.find((i) => i.element.placeholder === 'pve-c · CT 105')
    await locationInput.setValue('Synology DS923+')
    await save(wrapper)

    expect(patched[0].body.host_label).toBe('Synology DS923+')
    wrapper.unmount()
  })
})

describe('the category field offers the same suggestions as the add form', () => {
  // Reported 2026-08-07: the add form autocompleted and the edit form did not —
  // it was a bare <input> with no suggestions at all. Editing a service was
  // therefore the easiest way in the app to invent a duplicate category.
  beforeEach(() => {
    store.state.services = [
      { slug: 'a', name: 'A', category: 'Media', tags: [], status: 'up' },
      { slug: 'b', name: 'B', category: 'Utilities', tags: [], status: 'up' },
    ]
  })

  it('is a combobox, not a bare input', async () => {
    const wrapper = await openEditor()
    expect(wrapper.findAll('.combo').length).toBeGreaterThan(0)
    wrapper.unmount()
  })

  it('suggests the categories already in use, on focus', async () => {
    const wrapper = await openEditor()
    const combo = wrapper.findAll('.combo')[0]
    await combo.find('input').trigger('focus')
    expect(combo.findAll('.combo-option').map((o) => o.text())).toEqual([
      'Media',
      'Utilities',
    ])
    wrapper.unmount()
  })

  it('saves the picked suggestion', async () => {
    const wrapper = await openEditor()
    const combo = wrapper.findAll('.combo')[0]
    await combo.find('input').trigger('focus')
    await combo.findAll('.combo-option')[1].trigger('mousedown')
    await save(wrapper)
    expect(patched.at(-1).body.category).toBe('Utilities')
    wrapper.unmount()
  })
})

describe('opening straight into edit (the catalog Edit button)', () => {
  const buttons = (wrapper) => wrapper.findAll('button').map((b) => b.text())

  it('starts in the editor when asked to', () => {
    const wrapper = mount(ServiceRecord, {
      props: { service: record(), startEditing: true },
    })
    // The header's Edit/Delete/Open show in both modes; the editor card is
    // what swaps in, and Save is the button only it has.
    expect(buttons(wrapper)).toContain('Save')
    wrapper.unmount()
  })

  it('starts on the plain view by default', () => {
    const wrapper = mount(ServiceRecord, { props: { service: record() } })
    expect(buttons(wrapper)).not.toContain('Save')
    wrapper.unmount()
  })

  it('returns to the view on Cancel rather than staying in edit', async () => {
    const wrapper = mount(ServiceRecord, {
      props: { service: record(), startEditing: true },
    })
    await wrapper.findAll('button').find((b) => b.text() === 'Cancel').trigger('click')
    expect(buttons(wrapper)).not.toContain('Save')
    wrapper.unmount()
  })
})
