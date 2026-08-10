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
