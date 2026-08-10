<script setup>
/**
 * Write gate — spec §4c. "UI authentication is a write gate, not a door."
 *
 * There is no login screen and no interstitial. This modal appears only when a
 * write has already been attempted and refused. The token is exchanged via
 * POST /api/session for an httpOnly, SameSite=Strict cookie and is never
 * written to localStorage — the browser holds the session, JavaScript never
 * holds the credential.
 */
import { computed, nextTick, ref } from 'vue'

import { api } from '../api.js'
import BaseModal from './BaseModal.vue'

const props = defineProps({
  /** 'sign-in' | 'reauth'. Same field, different reason (§6b). */
  reason: { type: String, default: 'sign-in' },
})
const emit = defineEmits(['close', 'authenticated'])

const isReauth = computed(() => props.reason === 'reauth')
const title = computed(() =>
  isReauth.value ? 'Confirm with the admin token' : 'Admin token required',
)

const token = ref('')
const error = ref('')
const busy = ref(false)
const input = ref(null)

nextTick(() => input.value?.focus())

async function submit() {
  if (busy.value) return
  if (!token.value.trim()) {
    // Returning silently here reads as a dead button.
    error.value = 'Enter the admin token.'
    input.value?.focus()
    return
  }
  busy.value = true
  error.value = ''
  try {
    // Trimmed, because the emptiness check above trims: a token pasted with a
    // trailing newline otherwise fails with an opaque "Not authorised."
    // (audit informational). Whitespace is never part of the credential.
    await api.startSession(token.value.trim())
    // Drop the token from memory the moment the cookie exists.
    token.value = ''
    emit('authenticated')
  } catch (err) {
    error.value = err.message || 'Could not sign in.'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <BaseModal :title="title" @close="emit('close')">
    <div class="modal-body">
      <p
        v-if="isReauth"
        style="font: 500 12.5px/1.6 'Manrope', sans-serif; color: var(--txt2); margin: 0 0 16px"
      >
        This deletion is not reversible, and your session has been open for a
        while. Enter the admin token once more to confirm it. Adding and editing
        services carry on as normal.
      </p>
      <p
        v-else
        style="font: 500 12.5px/1.6 'Manrope', sans-serif; color: var(--txt2); margin: 0 0 16px"
      >
        Reading the catalog needs nothing. Changing it needs the admin token from
        your <code>.env</code>. It is exchanged for a session cookie, so you will
        not be asked again on this browser.
      </p>
      <div v-if="error" class="notice err">{{ error }}</div>
      <div class="field">
        <label class="field-label" for="admin-token">Admin token</label>
        <input
          id="admin-token"
          ref="input"
          v-model="token"
          class="mono"
          type="password"
          autocomplete="off"
          placeholder="paste the value of HOMELABVAULT_ADMIN_TOKEN"
          @keydown.enter="submit"
        />
      </div>
    </div>
    <div class="modal-foot">
      <span class="spacer" />
      <button class="btn-ghost" @click="emit('close')">Cancel</button>
      <button class="btn-primary" :disabled="busy" @click="submit">
        {{ busy ? 'Checking…' : isReauth ? 'Confirm' : 'Unlock' }}
      </button>
    </div>
  </BaseModal>
</template>
