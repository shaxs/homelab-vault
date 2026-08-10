<script setup>
/**
 * Icon chooser: auto-discovery, a URL, or a file from this machine.
 *
 * Whichever route is used, the image ends up cached under MEDIA_ROOT/icons (or
 * kept as an inline data URI). The remote URL is never stored on the record —
 * that would make the catalog fetch third-party images on every page load, and
 * the offline recovery export could not inline it (§9).
 */
import { ref } from 'vue'

import { api } from '../api.js'
import { withWriteGate } from '../store.js'

const props = defineProps({
  modelValue: { type: String, default: '' },
  fallback: { type: String, default: '?' },
  /** When set, "Refetch" re-runs discovery against this service's own URL. */
  slug: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const source = ref('')
const busy = ref('')
const error = ref('')
const fileInput = ref(null)

async function run(label, action) {
  busy.value = label
  error.value = ''
  try {
    const result = await withWriteGate(action)
    if (!result) {
      error.value = 'Unlock with the admin token first.'
      return
    }
    emit('update:modelValue', result.icon_url)
    source.value = ''
  } catch (err) {
    error.value = err.message || 'That did not work.'
  } finally {
    busy.value = ''
  }
}

const fromUrl = () =>
  source.value.trim() && run('url', () => api.fetchIcon(source.value.trim()))

const refetch = () =>
  run('refetch', () => api.refetchServiceIcon(props.slug))

function pickFile() {
  fileInput.value?.click()
}

function onFile(event) {
  const file = event.target.files?.[0]
  // Reset so choosing the same file twice still fires a change event.
  event.target.value = ''
  if (file) run('upload', () => api.uploadIcon(file))
}

function initials(text) {
  return (text || '?').replace(/[^A-Za-z0-9 ]/g, '').slice(0, 2).toUpperCase()
}
</script>

<template>
  <div class="field">
    <label class="field-label">Icon</label>
    <div class="icon-row">
      <span class="icon-preview">
        <img v-if="modelValue" :src="modelValue" alt="" />
        <template v-else>{{ initials(fallback) }}</template>
      </span>
      <input
        v-model="source"
        class="mono"
        placeholder="https://service.lab.local/favicon.ico"
        @keydown.enter.prevent="fromUrl"
      />
      <button class="btn-ghost" :disabled="!!busy" @click="fromUrl">
        {{ busy === 'url' ? '…' : 'Fetch' }}
      </button>
      <button
        v-if="slug"
        class="btn-ghost"
        title="Look again at the service's own page"
        :disabled="!!busy"
        @click="refetch"
      >
        {{ busy === 'refetch' ? '…' : '↻ Auto' }}
      </button>
      <button class="btn-ghost" :disabled="!!busy" @click="pickFile">
        {{ busy === 'upload' ? '…' : 'Upload…' }}
      </button>
      <button
        v-if="modelValue"
        class="clear-btn"
        title="Remove icon"
        @click="emit('update:modelValue', '')"
      >
        ✕
      </button>
      <input
        ref="fileInput"
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp,image/svg+xml,image/x-icon,.ico,.svg"
        hidden
        @change="onFile"
      />
    </div>
    <div v-if="error" class="field-hint" style="color: var(--err)">{{ error }}</div>
    <!-- Trimmed 2026-08-07. The caching-and-offline-export sentence was true and
         worth knowing, and it is not worth 14 words of grey text beside a field —
         it lives in the user guide instead. Density was as much of this form's
         legibility problem as contrast was. -->
    <div v-else class="field-hint">
      Found automatically when possible. <strong>↻ Auto</strong> retries,
      <strong>Fetch</strong> takes an image URL, <strong>Upload…</strong> takes a
      local file.
    </div>
  </div>
</template>
