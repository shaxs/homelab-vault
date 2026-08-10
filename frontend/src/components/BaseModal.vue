<script>
/**
 * Mounted instances, bottom → top. Module-level because the instances have no
 * other relationship to coordinate through: a WriteGate opening over a filled
 * AddService form is two siblings in App.vue, and each attaches its own
 * document-level keydown listener — so before this stack existed, one Escape
 * dismissed the token prompt AND destroyed the form beneath it, with
 * everything typed (audit S10). Only the top instance may act on Escape.
 */
const stack = []
</script>

<script setup>
/**
 * The shared modal. Per DESIGN_SYSTEM.md: never build a one-off custom modal —
 * every modal surface in the app (write gate, add service, bulk import, command
 * palette) composes this one.
 */
import { onMounted, onUnmounted } from 'vue'

defineProps({
  title: { type: String, default: '' },
  wide: { type: Boolean, default: false },
  bare: { type: Boolean, default: false },
})
const emit = defineEmits(['close'])

/** This instance's identity on the stack. */
const self = Symbol('BaseModal')

function onKey(event) {
  if (event.key !== 'Escape') return
  // Not the top modal: someone is stacked above us and Escape is theirs.
  if (stack[stack.length - 1] !== self) return
  emit('close')
}
onMounted(() => {
  stack.push(self)
  document.addEventListener('keydown', onKey)
})
onUnmounted(() => {
  // indexOf, not pop: modals can unmount out of order (the bottom one's
  // v-if can drop while a confirmation still shows above it).
  const index = stack.indexOf(self)
  if (index !== -1) stack.splice(index, 1)
  document.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div class="modal-backdrop" @mousedown.self="emit('close')">
    <div class="modal" :class="{ wide, palette: bare }">
      <div v-if="!bare" class="modal-head">
        <div class="modal-title">{{ title }}</div>
        <button class="modal-close" aria-label="Close" @click="emit('close')">✕</button>
      </div>
      <slot />
    </div>
  </div>
</template>
