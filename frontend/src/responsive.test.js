/**
 * The responsive layer's contract, enforced from the stylesheet itself.
 *
 * jsdom applies no stylesheets and does no layout, so the card/drawer/sheet
 * layouts cannot be asserted through mounted components — the DOM they
 * re-template is identical at every width by design. What CAN drift silently
 * is the stylesheet's own discipline, so like contrast.test.js this file reads
 * `styles.css` and checks the rules the responsive section promises to keep:
 *
 * 1. Exactly three media conditions exist — the two breakpoint widths and the
 *    hover capability. A fourth condition is how a breakpoint system decays
 *    into per-feature magic numbers.
 * 2. No `.drawer` rule says `display: none`. Below 1100px the detail drawer
 *    used to be display:none, which made node details unreachable on every
 *    tablet and phone; it overlays now, and this locks the regression out.
 * 3. `.app` and `.modal` carry a `dvh` height beside every `vh` one, so
 *    bottom-anchored UI tracks the real viewport as mobile browser chrome
 *    collapses — with `vh` kept as the fallback for browsers without `dvh`.
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import { describe, expect, it } from 'vitest'

const RAW = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), 'styles.css'),
  'utf-8',
)
/** Comments stripped, so prose about a rule can never satisfy or trip a check. */
const CSS = RAW.replace(/\/\*[\s\S]*?\*\//g, '')

describe('media query convention', () => {
  const ALLOWED = new Set([
    '(max-width: 1099.98px)', // tablet and down
    '(max-width: 719.98px)', // phone
    '(hover: none)', // touch capability
  ])
  const conditions = [...CSS.matchAll(/@media\s*([^{]+)\{/g)].map((m) => m[1].trim())

  it('uses only the two breakpoints and the hover capability', () => {
    for (const condition of conditions) {
      expect(
        ALLOWED.has(condition),
        `@media ${condition} is not one of the three permitted conditions — ` +
          `see the responsive section's header comment before adding a fourth`,
      ).toBe(true)
    }
  })

  it('has all three conditions in use — the convention is not vacuous', () => {
    expect(new Set(conditions)).toEqual(ALLOWED)
  })
})

describe('the detail drawer stays reachable', () => {
  it('no .drawer rule contains display: none', () => {
    // Selector/body pairs; @media preludes cannot leak into a selector group
    // because their own `{` terminates the preceding character class.
    for (const [, selector, body] of CSS.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      if (/\.drawer(?![\w-])/.test(selector)) {
        expect(
          /display:\s*none/.test(body),
          `"${selector.trim()}" hides the detail drawer — below the desktop ` +
            `breakpoint it must overlay, not vanish, or node details become ` +
            `unreachable on tablets and phones`,
        ).toBe(false)
      }
    }
  })
})

describe('viewport heights are dvh with a vh fallback', () => {
  function block(selector) {
    const start = CSS.indexOf(selector)
    expect(start, `${selector} rule missing from styles.css`).toBeGreaterThan(-1)
    return CSS.slice(start, CSS.indexOf('}', start))
  }

  it('.app declares 100dvh beside 100vh', () => {
    const app = block('.app {')
    expect(app).toContain('height: 100vh')
    expect(app).toContain('height: 100dvh')
  })

  it('.modal declares a dvh max-height beside the vh one', () => {
    const modal = block('.modal {')
    expect(modal).toContain('max-height: calc(100vh - 160px)')
    expect(modal).toContain('max-height: calc(100dvh - 160px)')
  })
})
