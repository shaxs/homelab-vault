/**
 * The text tokens must clear WCAG AA against every surface they sit on.
 *
 * This exists because "the form is hard to read in dark mode" turned out to be
 * measurable, not a matter of taste: `--txt3` was at 3.03:1 in dark and 2.72:1
 * in light, against a 4.5:1 floor for body text. Both had been that way since
 * v0, and the light theme was the worse of the two — which nobody noticed,
 * because the report came from a dark-mode session.
 *
 * A one-time correction would drift back the first time somebody nudged a grey
 * to taste. This reads the real token values out of `styles.css` and does the
 * arithmetic, so lowering one fails the suite instead of shipping.
 *
 * **It does not check that a given element uses a given token** — that is what
 * the component tests and a human's eyes are for. It checks that the tokens
 * themselves are usable.
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import { describe, expect, it } from 'vitest'

const CSS = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), 'styles.css'),
  'utf-8',
)

/** Pull the token block for `body` (light) or `body.dark`. */
function tokens(selector) {
  const start = CSS.indexOf(selector)
  const block = CSS.slice(start, CSS.indexOf('}', start))
  const found = {}
  for (const [, name, value] of block.matchAll(/(--[\w-]+):\s*(#[0-9a-fA-F]{3,8})\s*;/g)) {
    found[name] = value
  }
  return found
}

function relativeLuminance(hex) {
  let h = hex.replace('#', '')
  // `--panel: #fff` is real in the light block, so shorthand has to expand or
  // every channel parses as NaN and the whole theme "fails" for the wrong reason.
  if (h.length === 3) h = h.split('').map((c) => c + c).join('')
  const channels = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
  const [r, g, b] = channels.map((c) =>
    c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4,
  )
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(a, b) {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

/** AA for body text. Large/bold text may use 3:1, but these tokens carry prose. */
const AA = 4.5

//: Every surface text is rendered on. `--chip` is the worst case in both
//: themes, so it is the one to check when changing a grey.
const SURFACES = ['--bg', '--panel', '--panel2', '--chip']
const TEXTS = ['--txt', '--txt2', '--txt3']

describe.each([
  ['light', 'body {'],
  ['dark', 'body.dark {'],
])('%s theme text tokens', (theme, selector) => {
  const palette = tokens(selector)

  it('defines every token this test relies on', () => {
    for (const name of [...SURFACES, ...TEXTS]) {
      expect(palette[name], `${name} missing from the ${theme} block`).toBeTruthy()
    }
  })

  it.each(TEXTS)(`%s clears AA on every surface`, (text) => {
    for (const surface of SURFACES) {
      const ratio = contrast(palette[text], palette[surface])
      expect(
        ratio,
        `${text} (${palette[text]}) on ${surface} (${palette[surface]}) is ` +
          `${ratio.toFixed(2)}:1, below the ${AA}:1 AA floor — pick a value that ` +
          `clears it against --chip, which is the worst case`,
      ).toBeGreaterThanOrEqual(AA)
    }
  })
})

describe('--txt2 carries prose, so it holds a floor above AA', () => {
  /**
   * **This is a project decision, not the WCAG standard.** AA's 4.5:1 is
   * calibrated for roughly 16px text; `--txt2` carries 12px hint prose and
   * 10.5px tracked uppercase labels, and at those sizes the bare floor was
   * reported unreadable twice by the operator while measuring as "passing".
   *
   * 7:1 is AAA's figure for body text, used here as a proxy for "comfortable at
   * small sizes" rather than as an AAA claim about the whole app. Without this,
   * the value can drift back to a technically-conformant grey and the suite
   * would say nothing — which is exactly what happened at 6.48:1.
   */
  const PROSE_FLOOR = 7

  it.each([
    ['light', 'body {', '--panel'],
    ['dark', 'body.dark {', '--panel'],
    ['light', 'body {', '--chip'],
    ['dark', 'body.dark {', '--chip'],
  ])('%s: --txt2 clears %s on %s', (theme, selector, surface) => {
    const palette = tokens(selector)
    const ratio = contrast(palette['--txt2'], palette[surface])
    expect(
      ratio,
      `--txt2 (${palette['--txt2']}) on ${surface} is ${ratio.toFixed(2)}:1. ` +
        `It carries 12px prose, which wants ${PROSE_FLOOR}:1 here even though AA ` +
        `would accept 4.5 — see the note in this test before lowering it.`,
    ).toBeGreaterThanOrEqual(PROSE_FLOOR)
  })
})

describe('the greys stay ordered', () => {
  // If --txt3 ever became lighter than --txt2 in dark (or darker in light) the
  // visual hierarchy would inverit silently: de-emphasised text would read as
  // more important than secondary text.
  it('dark: --txt is brightest, then --txt2, then --txt3', () => {
    const d = tokens('body.dark {')
    expect(relativeLuminance(d['--txt'])).toBeGreaterThan(relativeLuminance(d['--txt2']))
    expect(relativeLuminance(d['--txt2'])).toBeGreaterThan(relativeLuminance(d['--txt3']))
  })

  it('light: --txt is darkest, then --txt2, then --txt3', () => {
    const l = tokens('body {')
    expect(relativeLuminance(l['--txt'])).toBeLessThan(relativeLuminance(l['--txt2']))
    expect(relativeLuminance(l['--txt2'])).toBeLessThan(relativeLuminance(l['--txt3']))
  })
})
