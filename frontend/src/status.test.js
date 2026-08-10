import { describe, expect, it } from 'vitest'

import { applyDerivedStatus, effectiveStatus } from './status.js'

const NOW = Date.parse('2026-08-02T12:00:00Z')
const agoSeconds = (s) => new Date(NOW - s * 1000).toISOString()

function service(overrides = {}) {
  return {
    slug: 'svc',
    health_enabled: true,
    health_status: 'up',
    health_interval_s: 60,
    health_checked_at: agoSeconds(10),
    status: 'up',
    ...overrides,
  }
}

describe('effectiveStatus', () => {
  it('reports the stored status for a recent check', () => {
    expect(effectiveStatus(service(), NOW)).toBe('up')
  })

  it('derives stale past three intervals', () => {
    // The reason this exists client-side: the API derives `stale` at serialize
    // time, so a tab left open would otherwise report "Up" forever after the
    // worker died.
    expect(effectiveStatus(service({ health_checked_at: agoSeconds(181) }), NOW)).toBe(
      'stale',
    )
  })

  it('does not go stale just inside the window', () => {
    expect(effectiveStatus(service({ health_checked_at: agoSeconds(179) }), NOW)).toBe(
      'up',
    )
  })

  it('honours a per-service interval', () => {
    const slow = service({ health_interval_s: 600, health_checked_at: agoSeconds(400) })
    expect(effectiveStatus(slow, NOW)).toBe('up')
  })

  it('follows the effective interval when the service pins none', () => {
    // Since v1.3 `health_interval_s` is null for a service that follows the
    // global setting, and the server sends the resolved number alongside it.
    // Reading only the raw column here would fall back to a hardcoded 60 and
    // call this service stale while the server calls it up — the same record
    // showing two different statuses depending on who you ask.
    const following = service({
      health_interval_s: null,
      health_interval_effective_s: 600,
      health_checked_at: agoSeconds(400),
    })
    expect(effectiveStatus(following, NOW)).toBe('up')
  })

  it('goes stale on the global interval too', () => {
    const following = service({
      health_interval_s: null,
      health_interval_effective_s: 30,
      health_checked_at: agoSeconds(100),
    })
    expect(effectiveStatus(following, NOW)).toBe('stale')
  })

  it('prefers a pinned interval over the global one', () => {
    const pinned = service({
      health_interval_s: 600,
      health_interval_effective_s: 600,
      health_checked_at: agoSeconds(400),
    })
    expect(effectiveStatus(pinned, NOW)).toBe('up')
  })

  it('marks a down service stale once the worker stops', () => {
    const s = service({ health_status: 'down', health_checked_at: agoSeconds(3600) })
    expect(effectiveStatus(s, NOW)).toBe('stale')
  })

  it('reports off when checks are disabled', () => {
    expect(effectiveStatus(service({ health_enabled: false }), NOW)).toBe('off')
  })

  it('reports unknown when never checked', () => {
    expect(effectiveStatus(service({ health_checked_at: null }), NOW)).toBe('unknown')
  })

  it('is idempotent — it reads stored fields, never a derived value', () => {
    const stale = service({ health_checked_at: agoSeconds(1000) })
    const once = effectiveStatus(stale, NOW)
    expect(effectiveStatus({ ...stale, status: once }, NOW)).toBe(once)
  })

  it('survives a malformed timestamp', () => {
    expect(effectiveStatus(service({ health_checked_at: 'not-a-date' }), NOW)).toBe('up')
  })
})

describe('applyDerivedStatus', () => {
  it('returns the same array identity when nothing changed', () => {
    const list = [service()]
    expect(applyDerivedStatus(list, NOW)).toBe(list)
  })

  it('returns a new array when a status moved', () => {
    const list = [service({ health_checked_at: agoSeconds(1000) })]
    const next = applyDerivedStatus(list, NOW)
    expect(next).not.toBe(list)
    expect(next[0].status).toBe('stale')
    expect(next[0].health_status).toBe('up') // stored value untouched
  })

  it('leaves unaffected rows untouched by identity', () => {
    const fresh = service({ slug: 'fresh' })
    const rotten = service({ slug: 'rotten', health_checked_at: agoSeconds(1000) })
    const next = applyDerivedStatus([fresh, rotten], NOW)
    expect(next[0]).toBe(fresh)
    expect(next[1]).not.toBe(rotten)
  })
})
