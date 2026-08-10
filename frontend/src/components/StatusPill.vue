<script setup>
/**
 * Status rendering — spec §8, step 3.2.
 *
 * `stale` means the health worker stopped, not that the service died, so it is
 * rendered visually distinct from `down` (amber, hollow dot) rather than as
 * another kind of red.
 */
import { computed } from 'vue'

import { now } from '../store.js'

const props = defineProps({
  service: { type: Object, required: true },
  note: { type: Boolean, default: true },
})

const LABELS = {
  up: 'Up',
  down: 'Down',
  stale: 'Stale',
  unknown: 'Unknown',
  off: 'No check',
}

const status = computed(() => props.service.status || 'unknown')
const label = computed(() => LABELS[status.value] || status.value)

/** `health_changed_at` gives "down for 3 days" without a history table (§8). */
const detail = computed(() => {
  const s = props.service
  if (status.value === 'down' && s.health_changed_at) {
    return `down for ${since(s.health_changed_at)}`
  }
  if (status.value === 'stale') return 'worker not reporting'
  if (status.value === 'unknown') return 'never checked'
  if (status.value === 'off') return 'checks disabled'
  if (status.value === 'up' && s.health_checked_at) {
    return `checked ${since(s.health_checked_at)} ago`
  }
  return ''
})

function since(iso) {
  // Reads the shared clock rather than Date.now() so the label actually counts
  // up on an open page instead of freezing at render time.
  const seconds = Math.max(0, (now.value - new Date(iso).getTime()) / 1000)
  if (seconds < 90) return `${Math.round(seconds)}s`
  const minutes = seconds / 60
  if (minutes < 90) return `${Math.round(minutes)}m`
  const hours = minutes / 60
  if (hours < 36) return `${Math.round(hours)}h`
  return `${Math.round(hours / 24)} days`
}
</script>

<template>
  <div>
    <span class="pill" :class="`st-${status}`">
      <span class="pill-dot" />
      <span class="pill-label">{{ label }}</span>
    </span>
    <div v-if="note && detail" class="pill-note">{{ detail }}</div>
  </div>
</template>
