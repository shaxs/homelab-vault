import { describe, expect, it } from 'vitest'

import {
  appendTerm,
  filterServices,
  formatTerm,
  fuzzyScore,
  hasTerm,
  paletteSearch,
  parseQuery,
  selectTerm,
  toggleTerm,
  tokenize,
} from './search.js'

const CATALOG = [
  {
    slug: 'jellyfin',
    name: 'Jellyfin',
    category: 'media',
    tags: ['video', 'transcode'],
    description: 'Movies and shows',
    host_label: 'pve-c · CT 117',
    status: 'up',
    provenance: 'manual',
  },
  {
    slug: 'karakeep',
    name: 'Karakeep',
    category: 'tools',
    tags: ['backup'],
    description: 'Bookmarks',
    host_label: 'pve-a · CT 121',
    status: 'down',
    provenance: 'api',
  },
  {
    slug: 'truenas',
    name: 'TrueNAS',
    category: 'storage',
    tags: ['backup', 'nas'],
    description: null,
    host_label: 'Synology DS923+',
    status: 'stale',
    provenance: 'manual',
  },
]

const slugs = (query) => filterServices(CATALOG, query).map((s) => s.slug)

describe('tokenize', () => {
  it('splits on whitespace', () => {
    expect(tokenize('a b  c')).toEqual(['a', 'b', 'c'])
  })

  it('keeps quoted phrases together', () => {
    expect(tokenize('cat:"home automation" up')).toEqual([
      'cat:home automation',
      'up',
    ])
  })

  it('handles an empty query', () => {
    expect(tokenize('')).toEqual([])
    expect(tokenize(undefined)).toEqual([])
  })
})

describe('bare words', () => {
  it('matches the name', () => {
    expect(slugs('jelly')).toEqual(['jellyfin'])
  })

  it('matches tags', () => {
    expect(slugs('transcode')).toEqual(['jellyfin'])
  })

  it('matches the description', () => {
    expect(slugs('bookmarks')).toEqual(['karakeep'])
  })

  it('is case-insensitive', () => {
    expect(slugs('JELLYFIN')).toEqual(['jellyfin'])
  })

  it('tolerates a null description', () => {
    expect(slugs('truenas')).toEqual(['truenas'])
  })
})

describe('key filters', () => {
  it('host: matches a substring of the location', () => {
    expect(slugs('host:pve-c')).toEqual(['jellyfin'])
  })

  it('cat: matches the category', () => {
    expect(slugs('cat:media')).toEqual(['jellyfin'])
  })

  it('tag: matches membership, not substring', () => {
    expect(slugs('tag:backup')).toEqual(['karakeep', 'truenas'])
    expect(slugs('tag:back')).toEqual([])
  })

  it('state: matches the derived status', () => {
    expect(slugs('state:down')).toEqual(['karakeep'])
    expect(slugs('state:stale')).toEqual(['truenas'])
  })

  it('src: matches provenance', () => {
    expect(slugs('src:api')).toEqual(['karakeep'])
  })

  it('an unknown key is treated as a bare word, not a filter', () => {
    expect(parseQuery('color:red')[0].type).toBe('text')
  })

  it('ignores a key with no value yet', () => {
    expect(parseQuery('tag:')).toEqual([])
    expect(slugs('tag:')).toEqual(['jellyfin', 'karakeep', 'truenas'])
  })
})

describe('negation', () => {
  it('state:!up excludes healthy services', () => {
    expect(slugs('state:!up')).toEqual(['karakeep', 'truenas'])
  })

  it('negates any key filter', () => {
    expect(slugs('tag:!backup')).toEqual(['jellyfin'])
    expect(slugs('cat:!media')).toEqual(['karakeep', 'truenas'])
  })
})

describe('multiple terms AND together', () => {
  it('combines a key filter and a negation', () => {
    // The spec's own example: "everything unhealthy on that node".
    expect(slugs('host:pve state:!up')).toEqual(['karakeep'])
  })

  it('combines a bare word with a key filter', () => {
    expect(slugs('backup src:manual')).toEqual(['truenas'])
  })

})

describe('empty query', () => {
  it('returns the whole catalog', () => {
    expect(slugs('')).toHaveLength(3)
    expect(slugs('   ')).toHaveLength(3)
  })
})

describe('fuzzyScore', () => {
  it('ranks a prefix best', () => {
    expect(fuzzyScore('jel', 'Jellyfin')).toBe(0)
  })

  it('matches a subsequence', () => {
    expect(fuzzyScore('jf', 'Jellyfin')).toBeGreaterThan(0)
  })

  it('rejects a non-match', () => {
    expect(fuzzyScore('zzz', 'Jellyfin')).toBe(-1)
  })
})

describe('paletteSearch', () => {
  it('finds a service by initials', () => {
    expect(paletteSearch(CATALOG, 'jf')[0].slug).toBe('jellyfin')
  })

  it('returns a shortlist when the query is empty', () => {
    expect(paletteSearch(CATALOG, '')).toHaveLength(3)
  })
})

describe('toggleTerm', () => {
  it('adds a term that is absent', () => {
    expect(toggleTerm('state:down', 'tag:media')).toBe('state:down tag:media')
  })

  it('removes a term that is present', () => {
    // Anything clickable that filters must be clickable again to unfilter.
    expect(toggleTerm('state:down tag:media', 'tag:media')).toBe('state:down')
  })

  it('round-trips back to the original query', () => {
    const start = 'host:pve-c state:!up'
    expect(toggleTerm(toggleTerm(start, 'cat:media'), 'cat:media')).toBe(start)
  })

  it('empties the query when the last term is removed', () => {
    expect(toggleTerm('state:up', 'state:up')).toBe('')
  })
})

describe('repeating a single-valued key ORs', () => {
  // A service has exactly one status, so ANDing `state:up state:down` can never
  // match anything — which cannot be what clicking both meant.
  it('state:up state:down returns both, not nothing', () => {
    expect(slugs('state:up state:down')).toEqual(['jellyfin', 'karakeep'])
  })

  it('cat: accumulates instead of cancelling out', () => {
    expect(slugs('cat:media cat:storage')).toEqual(['jellyfin', 'truenas'])
  })

  it('src: accumulates too', () => {
    expect(slugs('src:manual src:api')).toHaveLength(3)
  })

  it('still narrows to one when only one is given', () => {
    expect(slugs('state:up')).toEqual(['jellyfin'])
  })
})

describe('different keys still AND', () => {
  it('ORed states combine with another key by AND', () => {
    // "up or down" AND "in media" -> only jellyfin.
    expect(slugs('state:up state:down cat:media')).toEqual(['jellyfin'])
  })

  it('a bare word still narrows an ORed set', () => {
    expect(slugs('state:up state:down karakeep')).toEqual(['karakeep'])
  })
})

describe('tag: keeps AND semantics', () => {
  it('two tags mean the service has BOTH', () => {
    // Unlike status, a service really can have two tags, so "has both" is a
    // meaningful and useful query.
    expect(slugs('tag:backup tag:nas')).toEqual(['truenas'])
  })

  it('matches nothing when no service has both', () => {
    expect(slugs('tag:video tag:backup')).toEqual([])
  })
})

describe('negation always ANDs', () => {
  it('two negated states exclude both', () => {
    expect(slugs('state:!up state:!down')).toEqual(['truenas'])
  })

  it('a negation still applies alongside an ORed group', () => {
    expect(slugs('cat:media cat:tools state:!down')).toEqual(['jellyfin'])
  })
})

describe('hasTerm', () => {
  it('detects a present term exactly', () => {
    expect(hasTerm('tag:media', 'tag:media')).toBe(true)
    expect(hasTerm('tag:mediaserver', 'tag:media')).toBe(false)
  })
})

describe('status filtering end to end', () => {
  it('state:up narrows to healthy services and toggles back off', () => {
    let q = ''
    q = toggleTerm(q, 'state:up')
    expect(slugs(q)).toEqual(['jellyfin'])
    q = toggleTerm(q, 'state:up')
    expect(slugs(q)).toHaveLength(3)
  })

  it('state:down and state:stale each select their own rows', () => {
    expect(slugs('state:down')).toEqual(['karakeep'])
    expect(slugs('state:stale')).toEqual(['truenas'])
  })
})

describe('appendTerm', () => {
  it('appends a tag filter (§6b: clicking a tag appends tag:x)', () => {
    expect(appendTerm('state:down', 'tag:media')).toBe('state:down tag:media')
  })

  it('does not duplicate an existing term', () => {
    expect(appendTerm('tag:media', 'tag:media')).toBe('tag:media')
  })

  it('handles an empty starting query', () => {
    expect(appendTerm('', 'tag:media')).toBe('tag:media')
  })
})

describe('host: searches derived location too (v1.4)', () => {
  // Every fixture above sets `host_label`, which encodes the pre-v1.4 world
  // where a location was something the operator typed. Once Proxmox derives it,
  // **unpinned is the default** (§4a) — so the normal service has host_label
  // null and a derived value, and that shape was covered by nothing. The 45
  // tests above all passed while `host:pve-c3` matched nothing on a real
  // catalog.
  const DERIVED = [
    {
      slug: 'kan-bn',
      name: 'kan.bn',
      category: 'media',
      tags: [],
      description: null,
      host_label: null,
      location_derived: 'pve-c3 · CT 132',
      status: 'up',
      provenance: 'manual',
    },
    {
      slug: 'omniroute',
      name: 'OmniRoute',
      category: 'tools',
      tags: [],
      description: null,
      host_label: null,
      location_derived: 'pve-ca · CT 133',
      status: 'up',
      provenance: 'manual',
    },
    {
      slug: 'nas',
      name: 'NAS',
      category: 'storage',
      tags: [],
      description: null,
      host_label: 'Synology DS923+',
      location_derived: null,
      status: 'up',
      provenance: 'manual',
    },
  ]

  const slugs = (q) => filterServices(DERIVED, q).map((s) => s.slug)

  it('finds a service by its derived node when nothing is pinned', () => {
    expect(slugs('host:pve-c3')).toEqual(['kan-bn'])
  })

  it('still finds a service by its pinned value', () => {
    expect(slugs('host:synology')).toEqual(['nas'])
  })

  it('matches the VMID as well as the node', () => {
    // Quoted, because the tokenizer splits on whitespace — `host:CT 132` is
    // two terms. Same rule as `cat:"home automation"`.
    expect(slugs('host:"CT 132"')).toEqual(['kan-bn'])
    expect(slugs('host:132')).toEqual(['kan-bn'])
  })

  it('separates two nodes that share a prefix', () => {
    // "pve-c3" must not also match "pve-ca" — the names share "pve-c", and
    // neither is a substring of the other's full label.
    expect(slugs('host:pve-ca')).toEqual(['omniroute'])
  })

  it('negation works against the derived value', () => {
    expect(slugs('host:!pve-c3').sort()).toEqual(['nas', 'omniroute'])
  })

  it('a pinned value wins when both exist and differ', () => {
    const both = [{ ...DERIVED[0], host_label: 'pve-old · CT 132' }]
    // Both are searchable — the pin is what displays, but a search for either
    // should find it, or unpinning changes what your saved views return.
    expect(filterServices(both, 'host:pve-old')).toHaveLength(1)
    expect(filterServices(both, 'host:pve-c3')).toHaveLength(1)
  })
})

describe('multi-word values survive a round trip', () => {
  // Categories are free text and TagInput preserves inner spaces, so multi-word
  // values are a supported state. Every click surface used to emit them
  // unquoted, producing a query that matched nothing and could not be undone.
  const MULTI = [
    {
      slug: 'z2m',
      name: 'Zigbee2MQTT',
      category: 'home automation',
      tags: ['smart home'],
      description: '',
      status: 'up',
    },
  ]

  it('quotes the value, not the whole term', () => {
    expect(formatTerm('cat:home automation')).toBe('cat:"home automation"')
    expect(formatTerm('home automation')).toBe('"home automation"')
    expect(formatTerm('cat:media')).toBe('cat:media')
  })

  it('a clicked multi-word category actually filters to it', () => {
    const query = selectTerm('', 'cat:home automation')
    expect(query).toBe('cat:"home automation"')
    expect(filterServices(MULTI, query)).toHaveLength(1)
  })

  it('and can be clicked off again, which is what used to break', () => {
    // Unquoted, `hasTerm` could not recognise the query it had just produced,
    // so the chip never highlighted and a second click appended a duplicate.
    const query = selectTerm('', 'cat:home automation')
    expect(hasTerm(query, 'cat:home automation')).toBe(true)
    expect(selectTerm(query, 'cat:home automation')).toBe('')
  })

  it('the same holds for a multi-word tag, which still accumulates', () => {
    const query = toggleTerm('', 'tag:smart home')
    expect(query).toBe('tag:"smart home"')
    expect(filterServices(MULTI, query)).toHaveLength(1)
    expect(toggleTerm(query, 'tag:smart home')).toBe('')
  })
})

describe('selectTerm — single-select for click surfaces', () => {
  it('replaces a term of the same key rather than adding to it', () => {
    // The finding: clicking AI then Infrastructure used to show both.
    expect(selectTerm('cat:ai', 'cat:infra')).toBe('cat:infra')
  })

  it('clears when the active term is clicked again', () => {
    expect(selectTerm('cat:ai', 'cat:ai')).toBe('')
  })

  it('leaves terms of other keys alone', () => {
    expect(selectTerm('tag:video state:down cat:ai', 'cat:media')).toBe(
      'tag:video state:down cat:media',
    )
  })

  it('replaces a negated term of the same key too', () => {
    expect(selectTerm('cat:!ai', 'cat:media')).toBe('cat:media')
  })

  it('does not change what the query LANGUAGE means', () => {
    // The ruling was to change the click surface only. A typed query with two
    // cat: terms still ORs, so existing saved views keep matching what they
    // always matched.
    const both = filterServices(CATALOG, 'cat:media cat:storage')
    expect(both.map((s) => s.slug)).toEqual(['jellyfin', 'truenas'])
  })

  it('falls back to toggling for a term with no key', () => {
    expect(selectTerm('', 'jellyfin')).toBe('jellyfin')
    expect(selectTerm('jellyfin', 'jellyfin')).toBe('')
  })
})
