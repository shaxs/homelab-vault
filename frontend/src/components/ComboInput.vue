<script setup>
/**
 * Free text with suggestions, rendered by the app rather than the browser.
 *
 * **Why this exists.** Category and tag entry used a native `<datalist>`, which
 * technically worked and satisfied §6b's "free text with autocomplete against
 * values already in use" — but it looks and behaves like browser chrome, not
 * like this application. It has no affordance at all: no arrow, no hint that
 * suggestions exist, and in Chrome it only opens once you have typed. The label
 * said "autocomplete" and nothing on screen backed that up.
 *
 * So this is a combobox, not a select: **the typed value always wins**.
 * Categories are free text and always were — the suggestions exist to stop you
 * inventing `Media` a second time as `media`, not to constrain what you can
 * enter.
 *
 * Two deliberate behaviours worth keeping:
 *
 * - **Focus opens the whole list**, before you type anything. That is the
 *   discoverability the native control never had — you can see what already
 *   exists rather than guessing at a prefix.
 * - **Options commit on `mousedown`, not `click`.** `blur` fires before `click`,
 *   so a handler that closed the list on blur would unmount the option before
 *   the click ever landed, and picking with the mouse would silently do nothing.
 */
import { computed, ref } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  suggestions: { type: Array, default: () => [] },
  placeholder: { type: String, default: '' },
  id: { type: String, default: '' },
  /** Rendered when the field is empty and there is nothing to suggest. */
  emptyHint: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const open = ref(false)
const active = ref(-1)
const input = ref(null)

/**
 * Case-insensitive substring match, minus an exact hit on what is already
 * typed — offering the value you have just finished typing is noise.
 */
const matches = computed(() => {
  const typed = props.modelValue.trim().toLowerCase()
  return props.suggestions.filter((option) => {
    const value = String(option)
    if (value.toLowerCase() === typed) return false
    return !typed || value.toLowerCase().includes(typed)
  })
})

const visible = computed(() => open.value && matches.value.length > 0)

function show() {
  open.value = true
  active.value = -1
}

function hide() {
  open.value = false
  active.value = -1
}

function choose(value) {
  emit('update:modelValue', value)
  hide()
  input.value?.focus()
}

function onInput(event) {
  emit('update:modelValue', event.target.value)
  open.value = true
  active.value = -1
}

/**
 * Move the highlight, wrapping through "nothing highlighted" at both ends.
 *
 * There are `count + 1` states, not `count` — index `-1` means nothing is
 * highlighted, which is a real state you must be able to return to, or ↑ from
 * the first row traps you in the list. Shifting into `0..count` before the
 * modulo and back afterwards is what makes the arithmetic come out; doing it in
 * one expression on the raw `-1` is how the first ↓ ended up a no-op.
 */
function step(current, direction) {
  const count = matches.value.length
  if (!count) return -1
  return ((current + 1 + direction + count + 1) % (count + 1)) - 1
}

function onKeydown(event) {
  if (event.key === 'Escape') {
    // Close without clearing: the typed value is the user's, not ours.
    if (open.value) {
      event.stopPropagation()
      hide()
    }
    return
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    if (!open.value) return show()
    active.value = step(active.value, event.key === 'ArrowDown' ? 1 : -1)
    return
  }
  if (event.key === 'Enter' && visible.value && active.value >= 0) {
    // Only swallow Enter when a suggestion is actually highlighted, so Enter
    // still submits the surrounding form the rest of the time.
    event.preventDefault()
    choose(matches.value[active.value])
    return
  }
  if (event.key === 'Tab') hide()
}
</script>

<template>
  <div class="combo">
    <input
      :id="id"
      ref="input"
      role="combobox"
      autocomplete="off"
      :aria-expanded="visible"
      aria-autocomplete="list"
      :value="modelValue"
      :placeholder="placeholder"
      @input="onInput"
      @focus="show"
      @blur="hide"
      @keydown="onKeydown"
    />
    <ul v-if="visible" class="combo-list" role="listbox">
      <li
        v-for="(option, index) in matches"
        :key="option"
        class="combo-option"
        :class="{ active: index === active }"
        role="option"
        :aria-selected="index === active"
        @mousedown.prevent="choose(option)"
        @mouseenter="active = index"
      >
        {{ option }}
      </li>
    </ul>
    <div v-else-if="open && emptyHint && !suggestions.length" class="combo-empty">
      {{ emptyHint }}
    </div>
  </div>
</template>
