/**
 * Routing — v1 §8.
 *
 * The assertion that matters here is the one about **unknown paths**: a router
 * whose fallback is "show the catalog" turns a broken link into a working link
 * to the wrong page, which nobody reports as a bug because nothing looks wrong.
 */

import { beforeEach, describe, expect, it } from 'vitest'

import { navigate, pathFor, resolve, route, startRouter, stopRouter } from './router.js'

beforeEach(() => {
  window.history.replaceState({}, '', '/')
  route.value = resolve('/')
})

describe('resolve', () => {
  it('knows every screen the app owns', () => {
    expect(resolve('/').name).toBe('home')
    expect(resolve('/catalog').name).toBe('catalog')
    expect(resolve('/dashboard').name).toBe('dashboard')
    expect(resolve('/settings').name).toBe('settings')
  })

  it('pulls the slug out of a service URL', () => {
    expect(resolve('/services/jellyfin')).toMatchObject({
      name: 'record',
      slug: 'jellyfin',
    })
  })

  it('decodes an escaped slug', () => {
    expect(resolve('/services/pve%2Elab').slug).toBe('pve.lab')
  })

  it('ignores a trailing slash', () => {
    expect(resolve('/catalog/').name).toBe('catalog')
    expect(resolve('/services/jellyfin/').slug).toBe('jellyfin')
  })

  it('pulls the adapter out of a dashboard sub-route (TN4)', () => {
    expect(resolve('/dashboard/proxmox')).toMatchObject({
      name: 'dashboard',
      adapter: 'proxmox',
    })
    expect(resolve('/dashboard/truenas')).toMatchObject({
      name: 'dashboard',
      adapter: 'truenas',
    })
    // Bare /dashboard is the Overview — no adapter parameter.
    expect(resolve('/dashboard').adapter).toBeUndefined()
    // An unknown adapter still resolves to the dashboard (it degrades to
    // Overview client-side), never notFound — a stale bookmark stays useful.
    expect(resolve('/dashboard/foobar')).toMatchObject({
      name: 'dashboard',
      adapter: 'foobar',
    })
  })

  it('pulls the section out of a settings sub-route (TN-SET)', () => {
    expect(resolve('/settings/notifications')).toMatchObject({
      name: 'settings',
      section: 'notifications',
    })
    // Bare /settings is the first section — no section parameter.
    expect(resolve('/settings').section).toBeUndefined()
    // An unknown section still resolves to settings (it degrades to the first
    // section client-side), never notFound — same idiom as the adapter routes.
    expect(resolve('/settings/nonsense')).toMatchObject({
      name: 'settings',
      section: 'nonsense',
    })
  })

  it('says a path is unknown rather than falling back to the catalog', () => {
    expect(resolve('/nope').name).toBe('notFound')
    expect(resolve('/catalog/extra').name).toBe('notFound')
  })

  it('survives a malformed percent-escape instead of blanking the app', () => {
    // `decodeURIComponent('%zz')` throws URIError, and this module calls
    // `resolve()` at import time — uncaught, a cold load of a mistyped link
    // would stop main.js from ever mounting (audit S3). The raw segment is
    // kept as the slug so the "no service with that address" card renders.
    expect(resolve('/services/%zz')).toMatchObject({
      name: 'record',
      slug: '%zz',
    })
    expect(resolve('/services/100%')).toMatchObject({
      name: 'record',
      slug: '100%',
    })
  })
})

describe('pathFor', () => {
  it('builds the URL each screen is bookmarked at', () => {
    expect(pathFor('home')).toBe('/')
    expect(pathFor('catalog')).toBe('/catalog')
    expect(pathFor('record', { slug: 'jellyfin' })).toBe('/services/jellyfin')
  })

  it('escapes a slug so it cannot break out of its segment', () => {
    expect(pathFor('record', { slug: 'a/b' })).toBe('/services/a%2Fb')
  })

  it('builds and round-trips the dashboard adapter sub-routes (TN4)', () => {
    expect(pathFor('dashboard')).toBe('/dashboard')
    // The Overview pseudo-adapter is the bare route, not /dashboard/overview.
    expect(pathFor('dashboard', { adapter: 'overview' })).toBe('/dashboard')
    expect(pathFor('dashboard', { adapter: 'truenas' })).toBe('/dashboard/truenas')
    expect(resolve(pathFor('dashboard', { adapter: 'proxmox' })).adapter).toBe('proxmox')
  })

  it('builds and round-trips the settings section sub-routes (TN-SET)', () => {
    expect(pathFor('settings')).toBe('/settings')
    expect(pathFor('settings', { section: 'truenas' })).toBe('/settings/truenas')
    expect(resolve(pathFor('settings', { section: 'health-checks' })).section).toBe(
      'health-checks',
    )
  })
})

describe('navigate', () => {
  it('moves the URL and the route together', () => {
    navigate('dashboard')
    expect(window.location.pathname).toBe('/dashboard')
    expect(route.value.name).toBe('dashboard')
  })

  it('replaces rather than pushes when asked', () => {
    // `/` resolving to the landing page must not leave a history step that
    // Back immediately bounces off.
    const before = window.history.length
    navigate('catalog', {}, { replace: true })
    expect(window.location.pathname).toBe('/catalog')
    expect(window.history.length).toBe(before)
  })

  it('does not stack a second entry for the path already showing', () => {
    navigate('catalog')
    const length = window.history.length
    navigate('catalog')
    expect(window.history.length).toBe(length)
  })
})

describe('the back button', () => {
  it('re-renders, instead of moving the URL and leaving the page', () => {
    startRouter()
    try {
      navigate('dashboard')
      window.history.replaceState({}, '', '/settings')
      window.dispatchEvent(new PopStateEvent('popstate'))
      expect(route.value.name).toBe('settings')
    } finally {
      stopRouter()
    }
  })
})
