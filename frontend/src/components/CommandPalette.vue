<script setup>
/**
 * Command palette — spec §5. "This is the primary interaction and should be
 * reachable from any screen."
 *
 *   ⌘K  → fuzzy match
 *   ↵   → open the service's record
 *   ⌘↵  → launch the service in a tab
 *
 * DEVIATES from §5, which specifies "↵ opens in a named tab, ⌘↵ opens in a new
 * tab". That distinction turned out to be unobservable: named-target reuse does
 * not work in practice (verified against a real browser, including incognito),
 * so both keys did the same visible thing. Ruling 2026-08-03: give ↵ a distinct,
 * useful job instead — jump to the record — and let ⌘↵ launch.
 */
import { computed, nextTick, ref, watch } from 'vue'

import { paletteSearch } from '../search.js'
import { launch, state } from '../store.js'
import BaseModal from './BaseModal.vue'

const emit = defineEmits(['close', 'run-view', 'open-record'])

const query = ref('')
const cursor = ref(0)
const input = ref(null)

const services = computed(() => paletteSearch(state.services, query.value))
const views = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return state.views.slice(0, 3)
  return state.views.filter((v) => v.name.toLowerCase().includes(q)).slice(0, 3)
})
const rows = computed(() => [
  ...services.value.map((service) => ({ kind: 'service', service })),
  ...views.value.map((view) => ({ kind: 'view', view })),
])

watch(query, () => {
  cursor.value = 0
})

nextTick(() => input.value?.focus())

function move(delta) {
  const count = rows.value.length
  if (!count) return
  cursor.value = (cursor.value + delta + count) % count
}

function choose(row, launchIt) {
  if (!row) return
  if (row.kind === 'view') {
    emit('run-view', row.view.query)
  } else if (launchIt) {
    launch(row.service, true)
  } else {
    emit('open-record', row.service)
  }
  emit('close')
}

function onEnter(event) {
  choose(rows.value[cursor.value], event.metaKey || event.ctrlKey)
}
</script>

<template>
  <BaseModal bare @close="emit('close')">
    <input
      ref="input"
      v-model="query"
      class="palette-input"
      placeholder="Jump to a service…"
      autocomplete="off"
      @keydown.down.prevent="move(1)"
      @keydown.up.prevent="move(-1)"
      @keydown.enter.prevent="onEnter"
      @keydown.esc.prevent.stop="emit('close')"
    />
    <div style="overflow: auto; max-height: 50vh">
      <div v-if="services.length" class="palette-section">Services</div>
      <button
        v-for="(row, index) in rows"
        :key="row.kind + (row.service?.slug || row.view?.id)"
        class="palette-item"
        :class="{ sel: index === cursor }"
        @mouseenter="cursor = index"
        @click="choose(row, $event.metaKey || $event.ctrlKey)"
      >
        <template v-if="row.kind === 'service'">
          <span class="svc-icon" style="width: 24px; height: 24px; line-height: 24px; font-size: 9px">
            <img v-if="row.service.icon_url" :src="row.service.icon_url" alt="" />
            <template v-else>{{ row.service.name.slice(0, 2).toUpperCase() }}</template>
          </span>
          <span class="palette-name">{{ row.service.name }}</span>
          <span class="palette-meta">{{ row.service.category || '' }}</span>
        </template>
        <template v-else>
          <span class="palette-name">{{ row.view.name }}</span>
          <span class="palette-meta">view</span>
        </template>
      </button>
      <div v-if="!rows.length" style="padding: 24px 20px" class="spinner">
        Nothing matches “{{ query }}”.
      </div>
    </div>
    <div class="palette-foot">
      <span><span class="kbd">↵</span> open record</span>
      <span><span class="kbd">⌘↵</span> launch service</span>
      <span><span class="kbd">esc</span> close</span>
    </div>
  </BaseModal>
</template>
