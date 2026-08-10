/**
 * Search syntax — spec §6. "The most important feature in v0."
 *
 *   bare words   fuzzy match against name, tags, description
 *   host:pve-c  exact match on host_label substring
 *   cat:media    category
 *   tag:backup   tag membership
 *   state:down   health_status (the derived effective status)
 *   src:manual   provenance
 *   !            negates any key filter — state:!up
 *
 * Multiple terms AND together. A saved view is exactly this string, persisted
 * with a name.
 *
 * This runs entirely in the browser: the whole catalog ships on load (§3b), so
 * filtering is instant and there is no server round-trip.
 */

export const FIELD_KEYS = {
  host: 'host_label',
  cat: 'category',
  tag: 'tags',
  state: 'status',
  src: 'provenance',
}

export const SYNTAX_HELP = [
  { k: 'host:pve-c', d: 'location substring' },
  { k: 'cat:media', d: 'category' },
  { k: 'tag:backup', d: 'tag membership' },
  { k: 'state:down', d: 'up · down · stale · unknown · off' },
  { k: 'src:manual', d: 'manual · api · discovered · manifest' },
  { k: 'state:!up', d: 'negate any key filter' },
  { k: 'state:up state:down', d: 'repeat state, cat or src to match either' },
  { k: 'plex 4k', d: 'bare words fuzzy-match name, tags, description' },
]

/**
 * Split a query string into terms. Quoted phrases stay together so
 * `cat:"home automation"` works.
 */
export function tokenize(query) {
  const tokens = []
  // The optional `key:` prefix has to be part of the quoted alternative. Without
  // it, `\S+` wins the alternation at position 0 of `cat:"home automation"` and
  // splits it into `cat:"home` + `automation"`.
  const re = /([^\s"]*:)?"([^"]*)"|(\S+)/g
  let match
  while ((match = re.exec(query || '')) !== null) {
    const raw =
      match[2] !== undefined ? (match[1] || '') + match[2] : match[3]
    if (raw) tokens.push(raw)
  }
  return tokens
}

export function parseQuery(query) {
  const terms = []
  for (const token of tokenize(query)) {
    const colon = token.indexOf(':')
    const key = colon > 0 ? token.slice(0, colon).toLowerCase() : null

    if (key && key in FIELD_KEYS) {
      let value = token.slice(colon + 1)
      // Support both `!key:value` (handled below) and the documented
      // `key:!value` form from §6.
      let negated = false
      if (value.startsWith('!')) {
        negated = true
        value = value.slice(1)
      }
      // A bare `tag:` with no value filters nothing; drop it so the query stays
      // usable while it is still being typed.
      if (value === '') continue
      terms.push({ type: 'field', key, field: FIELD_KEYS[key], value, negated })
    } else {
      terms.push({ type: 'text', value: token.toLowerCase() })
    }
  }
  return terms
}

function fieldMatches(service, term) {
  const value = term.value.toLowerCase()

  if (term.key === 'tag') {
    return (service.tags || []).some((t) => String(t).toLowerCase() === value)
  }
  if (term.key === 'state') {
    return String(service.status || '').toLowerCase() === value
  }
  if (term.key === 'src') {
    return String(service.provenance || '').toLowerCase() === value
  }
  // host and cat match on substring — "exact match on host_label substring"
  // (§6), because a location reads "pve-c · CT 117" and you type "pve-c".
  //
  // `host:` searches the PINNED value and the DERIVED one (v1.4). Searching only
  // `host_label` meant `host:pve-c` matched nothing at all on a catalog where
  // nothing is pinned — which is the normal state now that Proxmox derives
  // location, and is exactly what "unpinned is the default" (§4a) produces.
  const raw =
    term.key === 'host'
      ? [service.host_label, service.location_derived].filter(Boolean).join(' ')
      : service[term.field]
  return String(raw == null ? '' : raw).toLowerCase().includes(value)
}

function textMatches(service, value) {
  const haystack = [
    service.name,
    service.slug,
    service.description,
    ...(service.tags || []),
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
  return haystack.includes(value)
}

/**
 * Keys naming an attribute a service has exactly ONE of. Repeating one of these
 * ORs rather than ANDs.
 *
 * §6 says "multiple terms AND together", and that is still true across
 * different keys and for `tag:`, where "has both" is exactly what you want. But
 * a service has a single status, so `state:up state:down` can never match
 * anything — ANDing repeated terms on a single-valued field is provably empty,
 * so it cannot be what the user meant by clicking both. `host:` stays ANDed
 * because it is a free-text substring match, where "contains both" is
 * meaningful.
 */
const OR_KEYS = new Set(['state', 'src', 'cat'])

/** True when a single service satisfies the query. */
export function matches(service, terms) {
  const groups = new Map()

  for (const term of terms) {
    if (term.type === 'text') {
      if (!textMatches(service, term.value)) return false
      continue
    }
    // Negation always ANDs: `state:!up state:!down` means neither.
    if (term.negated) {
      if (fieldMatches(service, term)) return false
      continue
    }
    if (!groups.has(term.key)) groups.set(term.key, [])
    groups.get(term.key).push(term)
  }

  for (const [key, group] of groups) {
    const satisfied = OR_KEYS.has(key)
      ? group.some((term) => fieldMatches(service, term))
      : group.every((term) => fieldMatches(service, term))
    if (!satisfied) return false
  }
  return true
}

export function filterServices(services, query) {
  const terms = parseQuery(query)
  if (!terms.length) return services
  return services.filter((service) => matches(service, terms))
}

/**
 * Subsequence scoring for the ⌘K palette (§5) — "jf" should find "Jellyfin".
 * Returns -1 for no match; lower scores rank better.
 */
export function fuzzyScore(needle, haystack) {
  const n = (needle || '').toLowerCase()
  const h = (haystack || '').toLowerCase()
  if (!n) return 0
  if (h.startsWith(n)) return 0
  const direct = h.indexOf(n)
  if (direct !== -1) return 1 + direct

  let score = 100
  let index = 0
  for (const char of n) {
    const found = h.indexOf(char, index)
    if (found === -1) return -1
    score += found - index
    index = found + 1
  }
  return score
}

export function paletteSearch(services, query) {
  if (!query.trim()) return services.slice(0, 8)
  return services
    .map((service) => ({
      service,
      score: Math.min(
        ...[service.name, service.slug, service.category || '']
          .map((field) => fuzzyScore(query, field))
          .map((s) => (s === -1 ? Infinity : s)),
      ),
    }))
    .filter((entry) => entry.score !== Infinity)
    .sort((a, b) => a.score - b.score)
    .slice(0, 8)
    .map((entry) => entry.service)
}

/**
 * Render one token back into query text, re-quoting it if it needs quoting.
 *
 * `tokenize` strips the quotes from `cat:"home automation"`, so a token held in
 * memory is always the unquoted form. Joining those back with a space is what
 * produced `cat:home automation` — which re-tokenizes to `cat:home` **plus** a
 * bare `automation`, matches nothing, and cannot be un-clicked because
 * `hasTerm` no longer recognises it. Categories are free text and tags keep
 * their inner spaces, so multi-word values are a supported state that every
 * click surface used to be unable to express.
 */
export function formatTerm(term) {
  if (!/\s/.test(term)) return term
  const colon = term.indexOf(':')
  // Quote the value, not the key: `cat:"home automation"`, never
  // `"cat:home automation"`, which tokenize would read as one bare word.
  if (colon > 0) return `${term.slice(0, colon + 1)}"${term.slice(colon + 1)}"`
  return `"${term}"`
}

const render = (tokens) => tokens.map(formatTerm).join(' ')

/** Append `tag:x` to a query without duplicating it (§6b). */
export function appendTerm(query, term) {
  const existing = tokenize(query)
  if (existing.includes(term)) return query
  return render(existing.concat(term))
}

export function removeTerm(query, term) {
  return render(tokenize(query).filter((t) => t !== term))
}

/**
 * Make `term` the only one of its key — single-select, for click surfaces where
 * accumulating is the wrong affordance.
 *
 * Categories use this: a service has exactly one category, so clicking AI and
 * then Infrastructure should show Infrastructure, not both. Clicking the active
 * one again clears it, because a filter you cannot turn off is a trap.
 *
 * **Tags deliberately do not use this** — a service genuinely has many tags, so
 * accumulating is correct there and `toggleTerm` stays their behaviour.
 *
 * This changes only what a *click* produces. The query language is untouched:
 * repeated `cat:` terms still OR together when typed, which keeps every saved
 * view working — a saved view is a stored query string, and changing what the
 * language means would silently alter what existing ones match.
 */
export function selectTerm(query, term) {
  const colon = term.indexOf(':')
  if (colon < 1) return toggleTerm(query, term)
  const key = term.slice(0, colon + 1)
  // Drop every term of this key, including a negated `cat:!x`.
  const others = tokenize(query).filter((t) => !t.startsWith(key))
  if (hasTerm(query, term)) return render(others)
  return render(others.concat(term))
}

export function hasTerm(query, term) {
  return tokenize(query).includes(term)
}

/**
 * Add a term, or take it away if it is already there. Anything clickable that
 * filters should be clickable again to unfilter — a chip you can only ever turn
 * on is a trap.
 */
export function toggleTerm(query, term) {
  return hasTerm(query, term) ? removeTerm(query, term) : appendTerm(query, term)
}
