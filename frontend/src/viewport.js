/**
 * Media-query state as a ref.
 *
 * The responsive layout is deliberately almost all CSS (see the responsive
 * section of styles.css); this composable exists for the one thing CSS cannot
 * do — reacting to a breakpoint crossing in component state. Its single
 * consumer is App.vue, which uses it to drop `nav-open` when the viewport
 * grows past the desktop breakpoint, so a drawer opened on a narrow window
 * cannot pop back open the next time the window is narrowed.
 *
 * Under jsdom `matchMedia` reports no match, so tests always run in the
 * desktop layout — which is also why the existing suites needed no changes
 * for the responsive work.
 */

import { getCurrentScope, onScopeDispose, ref } from 'vue'

export function useMediaQuery(query) {
  const matches = ref(false)
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return matches
  }
  const mql = window.matchMedia(query)
  matches.value = mql.matches
  const onChange = (event) => {
    matches.value = event.matches
  }
  // Older Safari only has addListener; feature-detect rather than assume.
  if (typeof mql.addEventListener === 'function') {
    mql.addEventListener('change', onChange)
    if (getCurrentScope()) {
      onScopeDispose(() => mql.removeEventListener('change', onChange))
    }
  } else if (typeof mql.addListener === 'function') {
    mql.addListener(onChange)
    if (getCurrentScope()) {
      onScopeDispose(() => mql.removeListener(onChange))
    }
  }
  return matches
}
