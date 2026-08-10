/**
 * Theme toggle.
 *
 * Persisted in localStorage. This does NOT violate spec §4b's "never
 * localStorage" rule: that rule governs *catalog data* — "saved views,
 * favorites, recents" — state that must survive across devices and therefore
 * belongs in a table. A per-device display preference is not in that set, and
 * the alternative (a settings table plus an unauthenticated write endpoint)
 * would be worse. Do not "fix" this by moving it server-side.
 */

import { ref, watchEffect } from 'vue'

const STORAGE_KEY = 'hlv-theme'

function initial() {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'light' || stored === 'dark') return stored
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light'
}

export const theme = ref(initial())

export function useTheme() {
  watchEffect(() => {
    document.body.classList.toggle('dark', theme.value === 'dark')
    localStorage.setItem(STORAGE_KEY, theme.value)
  })
  return theme
}
