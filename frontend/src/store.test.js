import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api } from './api.js'
import {
  cancelGate,
  loadCatalog,
  loadWorkers,
  resumeAfterUnlock,
  state,
  upsertLocal,
  withWriteGate,
} from './store.js'

function unauthorised() {
  return new ApiError(401, { detail: 'Unauthorized' })
}

beforeEach(() => {
  state.gateOpen = false
  state.authed = false
  cancelGate()
})

describe('catalog load generations (audit informational)', () => {
  // A slow in-flight poll used to land on top of whatever had happened while
  // it was in the air — a save's rename visibly reverted for up to one
  // interval, doubled by the visibilitychange refresh overlapping the timer.
  it('discards a stale in-flight poll instead of reverting a fresh save', async () => {
    let answerPoll
    vi.spyOn(api, 'listServices').mockReturnValue(
      new Promise((resolve) => {
        answerPoll = resolve
      }),
    )
    vi.spyOn(api, 'listViews').mockResolvedValue([])
    state.services = []

    const poll = loadCatalog({ quiet: true })
    // The save lands while the poll is still in the air…
    upsertLocal({ slug: 'jellyfin', name: 'Jellyfin (renamed)', tags: [], status: 'up' })
    // …and the poll finally answers, carrying the pre-save name.
    answerPoll([{ slug: 'jellyfin', name: 'Jellyfin', tags: [], status: 'up' }])
    await poll

    expect(state.services.find((s) => s.slug === 'jellyfin').name).toBe(
      'Jellyfin (renamed)',
    )
  })

  it('lets the newest of two overlapping polls win, whatever the arrival order', async () => {
    const answers = []
    vi.spyOn(api, 'listServices').mockImplementation(
      () =>
        new Promise((resolve) => {
          answers.push(resolve)
        }),
    )
    vi.spyOn(api, 'listViews').mockResolvedValue([])
    state.services = []

    const first = loadCatalog({ quiet: true })
    const second = loadCatalog({ quiet: true })
    // Arrival order inverted: the older request answers last.
    answers[1]([{ slug: 'a', name: 'Fresh', tags: [], status: 'up' }])
    await second
    answers[0]([{ slug: 'a', name: 'Stale', tags: [], status: 'up' }])
    await first

    expect(state.services[0].name).toBe('Fresh')
  })
})

describe('withWriteGate', () => {
  it('returns the result directly when the write is allowed', async () => {
    const result = await withWriteGate(async () => 'ok')
    expect(result).toBe('ok')
    expect(state.gateOpen).toBe(false)
  })

  it('opens the gate when the write is refused', async () => {
    withWriteGate(async () => {
      throw unauthorised()
    })
    await Promise.resolve()
    expect(state.gateOpen).toBe(true)
  })

  it('resolves the ORIGINAL caller with the retry result after unlocking', async () => {
    // The regression this guards: the gate used to re-run the parked action at
    // the App level and discard its return value, so the component that started
    // the write never received it and the UI silently did nothing.
    let attempt = 0
    const pending = withWriteGate(async () => {
      attempt += 1
      if (attempt === 1) throw unauthorised()
      return { ok: true, title: 'Jellyfin' }
    })

    await Promise.resolve()
    expect(state.gateOpen).toBe(true)

    await resumeAfterUnlock()

    await expect(pending).resolves.toEqual({ ok: true, title: 'Jellyfin' })
    expect(attempt).toBe(2)
    expect(state.gateOpen).toBe(false)
    expect(state.authed).toBe(true)
  })

  it('resolves undefined when the gate is cancelled', async () => {
    const pending = withWriteGate(async () => {
      throw unauthorised()
    })
    await Promise.resolve()

    cancelGate()

    await expect(pending).resolves.toBeUndefined()
    expect(state.gateOpen).toBe(false)
  })

  it('rejects rather than re-gating when the retry is still refused', async () => {
    // A second 401 means the scope is genuinely wrong (an agent attempting
    // DELETE); re-opening the gate would loop forever.
    const pending = withWriteGate(async () => {
      throw unauthorised()
    })
    await Promise.resolve()

    await resumeAfterUnlock()

    await expect(pending).rejects.toBeInstanceOf(ApiError)
    expect(state.gateOpen).toBe(false)
  })

  it('propagates non-auth errors instead of gating', async () => {
    await expect(
      withWriteGate(async () => {
        throw new ApiError(422, { detail: [{ loc: ['body', 'x'], msg: 'bad' }] })
      }),
    ).rejects.toBeInstanceOf(ApiError)
    expect(state.gateOpen).toBe(false)
  })

  it('is a no-op to resume when nothing is parked', async () => {
    await expect(resumeAfterUnlock()).resolves.toBeUndefined()
  })

  it('settles the previous caller when a second write is gated', async () => {
    // Opening the gate moves focus, which fires blur handlers that can start a
    // second gated write. The first caller must not be left hanging.
    const first = withWriteGate(async () => {
      throw unauthorised()
    })
    await Promise.resolve()

    const second = withWriteGate(async () => {
      throw unauthorised()
    })
    await Promise.resolve()

    await expect(first).resolves.toBeUndefined()

    await resumeAfterUnlock()
    await expect(second).rejects.toBeInstanceOf(ApiError)
  })
})

describe('ApiError', () => {
  it('flags 401 and 403 as needing auth', () => {
    expect(new ApiError(401, null).needsAuth).toBe(true)
    expect(new ApiError(403, null).needsAuth).toBe(true)
    expect(new ApiError(422, null).needsAuth).toBe(false)
  })

  it('names the offending field from a Ninja 422 body', () => {
    const err = new ApiError(422, {
      detail: [{ loc: ['body', 'payload', 'launch_target'], msg: 'not a valid choice' }],
    })
    expect(err.message).toContain('launch_target')
  })
})

describe('loadWorkers', () => {
  // `/healthz` answers 503 exactly when a worker is stalled (§5a) — which is
  // the one moment the sidebar indicator has to render. Letting the ApiError
  // propagate would blank it precisely then, so the body is read off the error.
  it('reads the body of a 503, because that is when it matters most', async () => {
    const stalled = {
      ok: false,
      workers: [
        { name: 'health', state: 'ok', stale: false },
        { name: 'proxmox', state: 'stale', stale: true },
      ],
    }
    vi.spyOn(api, 'getHealthz').mockRejectedValue(new ApiError(503, stalled))

    await loadWorkers()

    expect(state.workers.map((w) => w.name)).toEqual(['health', 'proxmox'])
    expect(state.workers.find((w) => w.name === 'proxmox').stale).toBe(true)
  })

  it('populates from a healthy 200 as well', async () => {
    vi.spyOn(api, 'getHealthz').mockResolvedValue({
      ok: true,
      workers: [{ name: 'health', state: 'ok', stale: false }],
    })

    await loadWorkers()

    expect(state.workers).toHaveLength(1)
  })

  it('never throws, and empties rather than keeping a stale list', async () => {
    // A network failure is not a worker report. Holding the last known list
    // would claim liveness we can no longer observe.
    vi.spyOn(api, 'getHealthz').mockRejectedValue(new TypeError('offline'))

    await expect(loadWorkers()).resolves.toBeUndefined()
    expect(state.workers).toEqual([])
  })
})
