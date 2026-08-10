/**
 * The API base — spec §3.
 *
 * `request()` in api.js is the single place the version prefix is written, and
 * all twenty exported methods route through it. That makes it the one line
 * whose breakage is total and whose breakage is invisible to every other test
 * here: store.test.js mocks `api.js` wholesale, so a wrong base would ship
 * green. These tests stub `fetch` instead and assert the URL that actually
 * goes out.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api.js'

function stubFetch() {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    text: async () => '[]',
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the version prefix', () => {
  it('is on every request, so the SPA never rides the redirect', async () => {
    const fetchMock = stubFetch()
    await api.listServices()
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/services', expect.any(Object))
  })

  it('appears exactly once — guards the double-prefix slip', async () => {
    const fetchMock = stubFetch()
    await api.listServices()
    const [url] = fetchMock.mock.calls[0]
    expect(url.match(/\/v1\//g)).toHaveLength(1)
  })

  it('survives path building for parameterised and query routes', async () => {
    const fetchMock = stubFetch()
    await api.getService('kara-keep')
    await api.slugPreview('https://kara-keep.lab.local')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/services/kara-keep')
    expect(fetchMock.mock.calls[1][0]).toBe(
      '/api/v1/slug-preview?launch_url=https%3A%2F%2Fkara-keep.lab.local',
    )
  })
})
