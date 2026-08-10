import { describe, expect, it } from 'vitest'

import {
  barWidth,
  bytes,
  bytesPair,
  load,
  percent,
  safeUrl,
  since,
  uptime,
} from './format.js'

describe('bytes', () => {
  it('prints whole bytes without decimals', () => {
    expect(bytes(0)).toBe('0 B')
    expect(bytes(512)).toBe('512 B')
  })
  it('steps up through binary units', () => {
    expect(bytes(1024)).toBe('1.00 KiB')
    expect(bytes(1099511627776)).toBe('1.00 TiB')
  })
  it('honours the requested precision', () => {
    expect(bytes(1073741824, 0)).toBe('1 GiB')
  })
  it('renders an em dash for a missing value', () => {
    expect(bytes(null)).toBe('—')
    expect(bytes(undefined)).toBe('—')
  })
})

describe('bytesPair', () => {
  it('states the unit once, so the pair is comparable', () => {
    expect(bytesPair(2431131811840, 4297390817280)).toBe('2.21 / 3.91 TiB')
  })
  it('renders an em dash when there is no capacity to divide by', () => {
    expect(bytesPair(0, 0)).toBe('—')
  })
})

describe('uptime', () => {
  it('leads with days on a long-running host', () => {
    expect(uptime(609946)).toBe('7d 01:25')
  })
  it('drops the days part under 24 hours', () => {
    expect(uptime(3660)).toBe('01:01')
    expect(uptime(0)).toBe('00:00')
  })
  it('renders an em dash for a missing value', () => {
    expect(uptime(null)).toBe('—')
  })
})

describe('percent', () => {
  it('rounds to one decimal by default', () => {
    expect(percent(18.75)).toBe('18.8%')
  })
  it('honours the requested precision', () => {
    expect(percent(5, 0)).toBe('5%')
  })
  it('renders an em dash for a missing value', () => {
    expect(percent(null)).toBe('—')
  })
})

describe('barWidth', () => {
  it('returns a css width', () => {
    expect(barWidth(42.5)).toBe('42.5%')
  })
  it('clamps, because a bar over 100% breaks the layout', () => {
    expect(barWidth(-3)).toBe('0%')
    expect(barWidth(150)).toBe('100%')
  })
  it('treats a non-finite value as empty', () => {
    expect(barWidth(null)).toBe('0%')
  })
})

describe('since', () => {
  const NOW = Date.parse('2026-08-07T02:00:00Z')
  const ago = (seconds) => new Date(NOW - seconds * 1000).toISOString()

  it('counts in seconds under a minute', () => {
    expect(since(ago(4), NOW)).toBe('4s ago')
  })
  it('counts in minutes, then hours, then days', () => {
    expect(since(ago(5 * 60), NOW)).toBe('5m ago')
    expect(since(ago(3 * 3600), NOW)).toBe('3h ago')
    expect(since(ago(5 * 86400), NOW)).toBe('5d ago')
  })
  it('says never rather than guessing at an unusable timestamp', () => {
    expect(since(null, NOW)).toBe('never')
    expect(since('not-a-date', NOW)).toBe('never')
  })
})

describe('safeUrl', () => {
  // From V1.5's api-security gate. `launch_url` is free text with no scheme
  // validation on the write path, and under `open-writes` anyone on the tailnet
  // can set it — so every place it becomes a link or a navigation checks first.
  it('passes an ordinary service URL through unchanged', () => {
    expect(safeUrl('https://jellyfin.lab.local:8096/web')).toBe(
      'https://jellyfin.lab.local:8096/web',
    )
    expect(safeUrl('http://192.168.0.10:8096')).toBe('http://192.168.0.10:8096')
  })

  it('refuses a javascript: URL, which executes on click in an href', () => {
    expect(safeUrl('javascript:alert(document.cookie)')).toBeNull()
    // Case and whitespace are not a defence anybody should rely on.
    expect(safeUrl('  JaVaScRiPt:alert(1)')).toBeNull()
  })

  it('refuses the other schemes that execute or exfiltrate', () => {
    expect(safeUrl('data:text/html,<script>alert(1)</script>')).toBeNull()
    expect(safeUrl('vbscript:msgbox(1)')).toBeNull()
    expect(safeUrl('file:///etc/passwd')).toBeNull()
  })

  it('refuses anything it cannot parse, and empty input', () => {
    expect(safeUrl('')).toBeNull()
    expect(safeUrl(null)).toBeNull()
    expect(safeUrl('http://')).toBeNull()
  })
})

describe('load', () => {
  it('always shows two decimals, including zero', () => {
    expect(load(0.42)).toBe('0.42')
    expect(load(0)).toBe('0.00')
  })
  it('renders an em dash for a missing value', () => {
    expect(load(null)).toBe('—')
  })
})
