<script setup>
/**
 * Chip-based tag entry with autocomplete against tags already in use (§6b:
 * "free text with autocomplete against values already in use").
 *
 * Replaces a comma-separated text field, which pushed the parsing onto the user
 * and gave no feedback about what had actually been captured — you could not
 * tell "a, b" from "a,b" from "a , b" without saving and looking.
 */
import { computed, ref } from 'vue'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  suggestions: { type: Array, default: () => [] },
  placeholder: { type: String, default: 'Add a tag…' },
})
const emit = defineEmits(['update:modelValue'])

const draft = ref('')
const input = ref(null)
const open = ref(false)
const active = ref(-1)

/** Only offer tags not already applied here. */
const available = computed(() =>
  props.suggestions.filter((tag) => !props.modelValue.includes(tag)),
)

/**
 * Suggestions, filtered by what has been typed.
 *
 * The native `<datalist>` this replaces did the filtering itself, but looked and
 * behaved like browser chrome rather than part of the app — and gave no hint
 * that anything was there until you had already typed. See `ComboInput`, which
 * is the same list for single-value fields; the two are kept separate because
 * this one's input sits inline among the chips.
 */
const matches = computed(() => {
  const typed = draft.value.trim().toLowerCase()
  return available.value.filter(
    (tag) => !typed || tag.toLowerCase().includes(typed),
  )
})

const visible = computed(() => open.value && matches.value.length > 0)

function normalise(value) {
  return value.trim().replace(/\s+/g, ' ')
}

/** Move the highlight, wrapping through -1 ("nothing highlighted"). See the
 *  identical helper in `ComboInput` for why the index is shifted first. */
function step(current, direction) {
  const count = matches.value.length
  if (!count) return -1
  return ((current + 1 + direction + count + 1) % (count + 1)) - 1
}

function commit(raw) {
  // Splitting on comma keeps paste-a-list working, which is how most people
  // arrive with tags in hand.
  const additions = raw
    .split(',')
    .map(normalise)
    .filter((tag) => tag && !props.modelValue.includes(tag))
  if (additions.length) {
    emit('update:modelValue', [...props.modelValue, ...additions])
  }
  draft.value = ''
}

function onKeydown(event) {
  if (event.key === 'Escape' && open.value) {
    event.stopPropagation()
    open.value = false
    active.value = -1
    return
  }
  if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && matches.value.length) {
    event.preventDefault()
    open.value = true
    active.value = step(active.value, event.key === 'ArrowDown' ? 1 : -1)
    return
  }
  // A highlighted suggestion takes Enter; otherwise it commits what was typed.
  if (event.key === 'Enter' && visible.value && active.value >= 0) {
    event.preventDefault()
    commit(matches.value[active.value])
    active.value = -1
    return
  }
  if (event.key === 'Enter' || event.key === ',' || event.key === 'Tab') {
    if (!draft.value.trim()) return
    // Enter must not submit the surrounding form, and Tab must not leave the
    // field, while there is an uncommitted tag sitting in the box.
    event.preventDefault()
    commit(draft.value)
    return
  }
  if (event.key === 'Backspace' && !draft.value && props.modelValue.length) {
    emit('update:modelValue', props.modelValue.slice(0, -1))
  }
}

function remove(tag) {
  emit('update:modelValue', props.modelValue.filter((t) => t !== tag))
  input.value?.focus()
}

/** Committing on blur stops a typed-but-unconfirmed tag being lost on save. */
function onBlur() {
  open.value = false
  active.value = -1
  if (draft.value.trim()) commit(draft.value)
}

/** Picked with the mouse. Fires on `mousedown`, before `blur` unmounts the row. */
function pick(tag) {
  commit(tag)
  active.value = -1
  input.value?.focus()
}
</script>

<template>
  <div class="tag-input" @click="input?.focus()">
    <span v-for="tag in modelValue" :key="tag" class="tag-token">
      {{ tag }}
      <button
        class="tag-token-x"
        :aria-label="`Remove ${tag}`"
        @click.stop="remove(tag)"
      >
        ✕
      </button>
    </span>
    <input
      ref="input"
      v-model="draft"
      role="combobox"
      autocomplete="off"
      :aria-expanded="visible"
      aria-autocomplete="list"
      :placeholder="modelValue.length ? '' : placeholder"
      @keydown="onKeydown"
      @focus="open = true"
      @blur="onBlur"
    />
    <ul v-if="visible" class="combo-list" role="listbox">
      <li
        v-for="(tag, index) in matches"
        :key="tag"
        class="combo-option"
        :class="{ active: index === active }"
        role="option"
        :aria-selected="index === active"
        @mousedown.prevent="pick(tag)"
        @mouseenter="active = index"
      >
        {{ tag }}
      </li>
    </ul>
  </div>
</template>
