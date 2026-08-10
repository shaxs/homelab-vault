import { beforeEach, describe, expect, it, vi } from 'vitest'

import { launch } from './store.js'

const service = (overrides = {}) => ({
  slug: 'jellyfin',
  name: 'Jellyfin',
  launch_url: 'https://jellyfin.lab.local/',
  launch_target: 'new_tab',
  ...overrides,
})

beforeEach(() => {
  window.open = vi.fn()
})

describe('launch', () => {
  it('always opens with noopener', () => {
    // THE regression that matters. Named targets were dropped (ruling
    // 2026-08-03) because reuse never worked in a real browser, and keeping
    // them required omitting noopener — paying a real reverse-tabnabbing
    // exposure for a feature that did nothing.
    launch(service())
    expect(window.open).toHaveBeenCalledWith(
      'https://jellyfin.lab.local/',
      '_blank',
      'noopener',
    )
  })

  it('never uses a named target', () => {
    launch(service())
    launch(service({ slug: 'sonarr', launch_url: 'https://sonarr/' }))

    for (const [, target, features] of window.open.mock.calls) {
      expect(target).toBe('_blank')
      expect(features).toBe('noopener')
    }
  })

  it('hardens the explicit new-tab path identically', () => {
    launch(service(), true)
    expect(window.open).toHaveBeenCalledWith(
      'https://jellyfin.lab.local/',
      '_blank',
      'noopener',
    )
  })

  it('navigates in place when the service prefers the same tab', () => {
    // jsdom refuses a real navigation, so just assert window.open was not used.
    const original = Object.getOwnPropertyDescriptor(window, 'location')
    delete window.location
    window.location = { href: '' }

    launch(service({ launch_target: 'same_tab' }))

    expect(window.location.href).toBe('https://jellyfin.lab.local/')
    expect(window.open).not.toHaveBeenCalled()
    if (original) Object.defineProperty(window, 'location', original)
  })

  it('still opens a new tab on Cmd+Enter even for a same_tab service', () => {
    launch(service({ launch_target: 'same_tab' }), true)
    expect(window.open).toHaveBeenCalledWith(
      'https://jellyfin.lab.local/',
      '_blank',
      'noopener',
    )
  })

  it('does nothing without a URL', () => {
    launch(service({ launch_url: '' }))
    expect(window.open).not.toHaveBeenCalled()
  })

  it('refuses to navigate to a javascript: URL', () => {
    // api-security gate, V1.5. `launch_url` is free text with no scheme check
    // on the write path and is anonymously settable under `open-writes`;
    // `location.href = 'javascript:…'` executes.
    launch(service({ launch_url: 'javascript:alert(document.cookie)' }))
    expect(window.open).not.toHaveBeenCalled()
  })
})
