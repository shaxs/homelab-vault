<script setup>
/**
 * Rename or remove a category/tag across the whole catalog.
 *
 * Spec §6b says these values need no administration — "a value exists because a
 * service uses it and disappears when none do" — which is true of their
 * lifecycle but leaves a typo unfixable except one service at a time. This is a
 * bulk edit reached from where the value already appears, not an administration
 * screen, so §5's "no settings screen" still holds.
 */
import { computed, nextTick, ref } from 'vue'

import { api } from '../api.js'
import { state, withWriteGate } from '../store.js'
import BaseModal from './BaseModal.vue'

const props = defineProps({
  kind: { type: String, required: true }, // 'category' | 'tag' | 'view'
  name: { type: String, required: true },
})
const emit = defineEmits(['close', 'done'])

/**
 * A saved view is renamed here too, but it is not taxonomy and the differences
 * matter:
 *
 * - **It has no usage count.** A category is used by N services; a view is one
 *   row holding one query, so "used by 3 services" would be a fabrication.
 * - **A name collision is refused, not merged.** Merging two spellings of a
 *   category is the point; merging two views would silently discard one of the
 *   queries, so the API answers 422 and the dialog shows it.
 */
const isView = computed(() => props.kind === 'view')

const next = ref(props.name)
const busy = ref(false)
const error = ref('')
const input = ref(null)

nextTick(() => input.value?.select())

/** Show the blast radius before the change, not after. */
const usageCount = computed(() => {
  if (isView.value) return 0
  return props.kind === 'category'
    ? state.services.filter((s) => s.category === props.name).length
    : state.services.filter((s) => (s.tags || []).includes(props.name)).length
})

const merges = computed(() => {
  const target = next.value.trim()
  if (!target || target === props.name || isView.value) return false
  return props.kind === 'category'
    ? state.services.some((s) => s.category === target)
    : state.services.some((s) => (s.tags || []).includes(target))
})

async function rename() {
  const target = next.value.trim()
  if (!target) {
    error.value = 'Enter a name.'
    return
  }
  if (target === props.name) {
    emit('close')
    return
  }
  if (isView.value) {
    await run(
      () => api.renameView(props.name, target),
      (result) => `View renamed to “${result.name}”.`,
    )
    return
  }
  await run(
    () =>
      props.kind === 'category'
        ? api.renameCategory(props.name, target)
        : api.renameTag(props.name, target),
    (result) => `Renamed to “${result.name}” on ${result.affected} service(s).`,
  )
}

async function remove() {
  if (isView.value) {
    if (!window.confirm(`Delete the view “${props.name}”?`)) return
    await run(() => api.deleteView(props.name), () => 'View deleted.')
    return
  }
  const noun = props.kind === 'category' ? 'category' : 'tag'
  if (
    !window.confirm(
      `Remove the ${noun} “${props.name}” from ${usageCount.value} service(s)? ` +
        `The services themselves are not deleted.`,
    )
  ) {
    return
  }
  await run(
    () =>
      props.kind === 'category'
        ? api.clearCategory(props.name)
        : api.deleteTag(props.name),
    (result) => `Removed from ${result.affected} service(s).`,
  )
}

async function run(action, describe) {
  busy.value = true
  error.value = ''
  try {
    const result = await withWriteGate(action)
    if (!result) return // gate cancelled
    emit('done', describe(result))
    emit('close')
  } catch (err) {
    error.value = err.message || 'Could not apply that change.'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <BaseModal
    :title="{ category: 'Edit category', tag: 'Edit tag', view: 'Edit saved view' }[kind]"
    @close="emit('close')"
  >
    <div class="modal-body">
      <div v-if="error" class="notice err">{{ error }}</div>

      <div class="field">
        <label class="field-label" for="taxonomy-name">Name</label>
        <input
          id="taxonomy-name"
          ref="input"
          v-model="next"
          class="mono"
          @keydown.enter="rename"
        />
        <div v-if="isView" class="field-hint">
          A saved view is a stored query. Renaming changes only its label — the
          query it runs is unchanged.
        </div>
        <div v-else class="field-hint">
          Used by {{ usageCount }} service(s). Renaming updates every one of them.
        </div>
      </div>

      <div v-if="merges" class="notice warn">
        “{{ next.trim() }}” already exists — these will be merged into one.
      </div>
    </div>

    <div class="modal-foot">
      <button class="btn-ghost btn-danger" :disabled="busy" @click="remove">
        {{ isView ? 'Delete view' : 'Remove from all' }}
      </button>
      <span class="spacer" />
      <button class="btn-ghost" @click="emit('close')">Cancel</button>
      <button class="btn-primary" :disabled="busy" @click="rename">
        {{ busy ? 'Applying…' : 'Rename' }}
      </button>
    </div>
  </BaseModal>
</template>
