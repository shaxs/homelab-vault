/**
 * The client half of settings and auth modes — v1 §6a, §6b, §6c.
 *
 * Two of the three assertions here are about *failing safe in the UI*, which is
 * a different question from failing safe on the server and is not covered by
 * any backend test: if the mode cannot be read, the badge must not imply writes
 * are open; and a re-prompt must not tell an operator who is demonstrably
 * signed in that they are not signed in.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api } from './api.js'
import { cancelGate, loadPublicSettings, state, withWriteGate } from './store.js'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

beforeEach(() => {
  state.authMode = 'full'
  state.publicSettings = {}
  cancelGate()
})

describe('the settings endpoints', () => {
  function stubFetch(bodyText = '{}') {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => bodyText,
    })
    vi.stubGlobal('fetch', fetchMock)
    return fetchMock
  }

  it('are reached under the version prefix like everything else', async () => {
    const fetchMock = stubFetch()
    await api.getSettings()
    await api.getPublicSettings()
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/settings')
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/settings/public')
  })

  it('sends a settings write as a flat key/value body', async () => {
    const fetchMock = stubFetch()
    await api.updateSettings({ 'health.default_interval_s': 45 })
    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/settings')
    expect(options.method).toBe('PATCH')
    expect(JSON.parse(options.body)).toEqual({ 'health.default_interval_s': 45 })
  })
})

describe('the auth mode badge', () => {
  it('reports the mode the server is actually in', async () => {
    vi.spyOn(api, 'getPublicSettings').mockResolvedValue({
      auth_mode: 'open-writes',
      values: { 'ui.landing_page': 'catalog' },
    })
    await loadPublicSettings()
    expect(state.authMode).toBe('open-writes')
    expect(state.publicSettings['ui.landing_page']).toBe('catalog')
  })

  it('falls back to `full` when the mode cannot be read', async () => {
    // Failing the other way would paint a reassuring "writes are open" badge
    // during an outage, which is the one reading we must never invent.
    state.authMode = 'open'
    vi.spyOn(api, 'getPublicSettings').mockRejectedValue(new Error('offline'))
    await loadPublicSettings()
    expect(state.authMode).toBe('full')
  })

  it('never throws, so a failure cannot stop the catalog rendering', async () => {
    vi.spyOn(api, 'getPublicSettings').mockRejectedValue(new ApiError(500, null))
    await expect(loadPublicSettings()).resolves.toBeUndefined()
  })
})

describe('the destructive-operation re-prompt', () => {
  it('is told apart from "not signed in" by the response code', () => {
    const reauth = new ApiError(403, { detail: 'confirm', code: 'reauth_required' })
    const csrf = new ApiError(403, { detail: 'CSRF verification failed.' })
    const anonymous = new ApiError(401, { detail: 'Unauthorized' })

    expect(reauth.needsReauth).toBe(true)
    expect(csrf.needsReauth).toBe(false)
    expect(anonymous.needsReauth).toBe(false)
    // All three still open the same modal — only the words differ.
    expect([reauth.needsAuth, csrf.needsAuth, anonymous.needsAuth]).toEqual([
      true,
      true,
      true,
    ])
  })

  it('opens the gate in reauth mode', async () => {
    withWriteGate(async () => {
      throw new ApiError(403, { detail: 'confirm', code: 'reauth_required' })
    })
    await Promise.resolve()
    expect(state.gateOpen).toBe(true)
    expect(state.gateReason).toBe('reauth')
  })

  it('opens the gate in sign-in mode for an ordinary refusal', async () => {
    withWriteGate(async () => {
      throw new ApiError(401, { detail: 'Unauthorized' })
    })
    await Promise.resolve()
    expect(state.gateOpen).toBe(true)
    expect(state.gateReason).toBe('sign-in')
  })

  it('resets the reason when the gate is dismissed', async () => {
    withWriteGate(async () => {
      throw new ApiError(403, { detail: 'confirm', code: 'reauth_required' })
    })
    await Promise.resolve()
    cancelGate()
    expect(state.gateReason).toBe('sign-in')
  })
})
