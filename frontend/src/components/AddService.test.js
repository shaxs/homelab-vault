/**
 * The add form's location preview — v1 §4a, §7b.
 *
 * **Why this file exists.** The operator's first real use of V1.4 was adding a
 * service and expecting the Location field to fill itself in. It did not, and
 * could not: that field is the *pinned* value, so pre-filling it would pin every
 * service at creation and the derived location could never track again. But the
 * empty field read as broken, which is a real defect even though the behaviour
 * was correct.
 *
 * So the two assertions that matter here are a pair, and neither is sufficient
 * alone: the derived location **is shown**, and it is **not written into the
 * input**. A test for only the first would pass a version that pins everything.
 *
 * `AddService.vue` had no test at all before this — the same gap
 * `ServiceRecord.vue` had until V1.3's manual pass found two bugs in it.
 */

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api.js'
import * as store from '../store.js'
import AddService from './AddService.vue'

const PROBE = {
  ok: true,
  url: 'http://192.168.0.42:3000/',
  final_url: 'http://192.168.0.42:3000/login',
  status: 200,
  title: 'Login | kan.bn',
  icon_url: null,
  redirects: [],
  error: null,
  suggested_slug: 'kan-bn',
  suggested_name: 'kan.bn',
  derived_location: 'pve-c · CT 132',
  derived_location_provider: 'proxmox',
}

async function probeWith(result) {
  vi.spyOn(api, 'probe').mockResolvedValue(result)
  vi.spyOn(store, 'withWriteGate').mockImplementation((action) => action())

  const wrapper = mount(AddService)
  // "More options" — the mode that shows the Location field at all.
  await wrapper.findAll('button').find((b) => b.text() === 'More options').trigger('click')
  const urlInput = wrapper.find('input')
  await urlInput.setValue(result.url)
  // The form probes on Enter (`@keydown.enter`) or blur — not keyup.
  await urlInput.trigger('keydown.enter')
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

function locationInput(wrapper) {
  return wrapper.find('#svc-host')
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('the location preview on the add form', () => {
  it('shows the location the service would get', async () => {
    const wrapper = await probeWith(PROBE)
    expect(wrapper.text()).toContain('pve-c · CT 132')
    expect(wrapper.text()).toContain('from proxmox')
    wrapper.unmount()
  })

  it('does NOT put it in the Location input', async () => {
    // The whole point. Pre-filling would pin every service at creation.
    const wrapper = await probeWith(PROBE)
    expect(locationInput(wrapper).element.value).toBe('')
    wrapper.unmount()
  })

  it('says what leaving the field empty will do', async () => {
    const wrapper = await probeWith(PROBE)
    expect(locationInput(wrapper).attributes('placeholder')).toContain('pve-c · CT 132')
    expect(wrapper.text()).toMatch(/pins/i)
    wrapper.unmount()
  })

  it('shows no location row when nothing matched', async () => {
    // A service outside the cluster, an ambiguous address, or no snapshot yet —
    // all arrive as null, and an empty row would read as a failure.
    const wrapper = await probeWith({
      ...PROBE,
      derived_location: null,
      derived_location_provider: null,
    })
    expect(wrapper.text()).not.toContain('from proxmox')
    expect(locationInput(wrapper).attributes('placeholder')).toContain(
      'leave empty to follow Proxmox',
    )
    wrapper.unmount()
  })

  it('a typed value is still what gets sent', async () => {
    // Pinning deliberately stays available — it is just never automatic.
    const wrapper = await probeWith(PROBE)
    await locationInput(wrapper).setValue('Synology DS923+')

    const posted = []
    vi.spyOn(api, 'upsertService').mockImplementation(async (body) => {
      posted.push(body)
      return { slug: 'kan-bn', ...body }
    })
    vi.spyOn(store, 'upsertLocal').mockImplementation(() => {})
    await wrapper.findAll('button').find((b) => b.text() === 'Save service').trigger('click')
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(posted[0].host_label).toBe('Synology DS923+')
    wrapper.unmount()
  })
})

describe('bulk rows are keyed by identity, not URL (audit informational)', () => {
  async function flush(wrapper, rounds = 4) {
    for (let i = 0; i < rounds; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0))
      await wrapper.vm.$nextTick()
    }
  }

  it('keeps a failed duplicate URL on review when its twin imports', async () => {
    // The same URL pasted twice is two legitimate rows once their slugs are
    // made distinct. Keying rows and the post-import cleanup on `row.url`
    // dropped BOTH copies when either landed — hiding exactly the row that
    // still needed attention.
    vi.spyOn(store, 'withWriteGate').mockImplementation((action) => action())
    vi.spyOn(store, 'upsertLocal').mockImplementation(() => {})
    vi.spyOn(api, 'probe')
      .mockResolvedValueOnce({ ...PROBE, suggested_slug: 'svc-a', suggested_name: 'A' })
      .mockResolvedValueOnce({ ...PROBE, suggested_slug: 'svc-b', suggested_name: 'B' })
    vi.spyOn(api, 'upsertService')
      .mockImplementationOnce(async (body) => ({ slug: body.slug, ...body }))
      .mockImplementationOnce(async () => {
        throw new Error('boom')
      })

    const wrapper = mount(AddService)
    await wrapper.findAll('button').find((b) => b.text() === 'Bulk import').trigger('click')
    await wrapper
      .find('#bulk-urls')
      .setValue('http://192.168.0.42:3000\nhttp://192.168.0.42:3000')
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Probe and review'))
      .trigger('click')
    await flush(wrapper)
    expect(wrapper.findAll('.bulk-row')).toHaveLength(2)

    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Import'))
      .trigger('click')
    await flush(wrapper)

    // The successful twin left the list; the failed one is still there to fix.
    const rows = wrapper.findAll('.bulk-row')
    expect(rows).toHaveLength(1)
    expect(rows[0].find('.bulk-input').element.value).toBe('svc-b')
    wrapper.unmount()
  })
})
