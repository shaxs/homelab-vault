<script setup>
/**
 * Service record — spec §5: "overview only — record, health, edit."
 */
import { computed, ref } from 'vue'

import { api } from '../api.js'
import {
  allCategories,
  allTags,
  launch,
  recheckHealth,
  removeLocal,
  upsertLocal,
  withWriteGate,
} from '../store.js'
import ComboInput from './ComboInput.vue'
import IconPicker from './IconPicker.vue'
import StatusPill from './StatusPill.vue'
import TagInput from './TagInput.vue'

const props = defineProps({
  service: { type: Object, required: true },
  // Arrive already editing — the catalog's Edit button. Read once on mount,
  // so Save/Cancel return to the normal view rather than re-entering edit.
  startEditing: { type: Boolean, default: false },
})
const emit = defineEmits(['back', 'changed'])

const editing = ref(false)
const draft = ref({})
const busy = ref(false)
const error = ref('')
const rechecking = ref(false)

/* --- Location: manual vs derived (§4a) ------------------------------------
 *
 * Two values, never merged. `host_label` is the operator's pin; enrichment
 * writes only the link, and the API returns both plus whether they differ.
 *
 * **There is deliberately no "Accept" button.** Accept would write the derived
 * value into the pin — which re-pins, so the pair diverges again on the next
 * migration and the operator accepts forever. It looks like resolution and is a
 * treadmill. The one correct action is to stop pinning, so `unpin` is the only
 * affordance here. Deliberate re-pinning stays available through the ordinary
 * Location field in Edit, just not labelled "accept". */
const pinned = computed(() => (props.service.host_label || '').trim())
const derived = computed(() => props.service.location_derived || '')
const disagrees = computed(() => props.service.location_disagrees === true)

async function unpin() {
  busy.value = true
  error.value = ''
  try {
    // No new endpoint — PATCH already clears it (§4a, 2026-08-03 correction).
    const saved = await withWriteGate(() =>
      api.patchService(props.service.slug, { host_label: null }),
    )
    if (!saved) return
    upsertLocal(saved)
    emit('changed')
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

async function recheck() {
  rechecking.value = true
  error.value = ''
  try {
    await recheckHealth(props.service.slug)
  } catch (err) {
    error.value = err.message || 'Could not run the check.'
  } finally {
    rechecking.value = false
  }
}

function startEdit() {
  draft.value = {
    name: props.service.name,
    icon_url: props.service.icon_url || '',
    category: props.service.category || '',
    host_label: props.service.host_label || '',
    description: props.service.description || '',
    launch_url: props.service.launch_url,
    health_url: props.service.health_url || '',
    tags: [...(props.service.tags || [])],
    health_enabled: props.service.health_enabled,
    notify_enabled: props.service.notify_enabled,
    // Held as strings so an EMPTY field stays empty. `null` is a real value
    // here — it means "follow the global setting" — so it has to survive a
    // round trip through an <input>, which turns everything into text.
    health_interval_s: props.service.health_interval_s ?? '',
    health_fail_threshold: props.service.health_fail_threshold ?? '',
  }
  editing.value = true
}

/** Milliseconds shown as seconds, because every other duration on the health
 *  panel is seconds. `timeout 3000ms` beside `1000s` is technically right and
 *  reads as the larger of the two, which is the opposite of true. */
function asSeconds(ms) {
  const value = Number(ms)
  if (!Number.isFinite(value)) return '—'
  return value % 1000 === 0 ? `${value / 1000}s` : `${(value / 1000).toFixed(1)}s`
}

/** Empty field -> null, which the API reads as "follow the global setting".
 *  Anything else -> a number, which pins this service to it. */
function blankToNull(value) {
  const text = String(value ?? '').trim()
  if (text === '') return null
  const number = Number(text)
  return Number.isFinite(number) ? number : null
}

async function save() {
  busy.value = true
  error.value = ''
  const body = {
    name: draft.value.name,
    icon_url: draft.value.icon_url || null,
    category: draft.value.category || null,
    host_label: draft.value.host_label || null,
    description: draft.value.description || null,
    launch_url: draft.value.launch_url,
    health_url: draft.value.health_url || null,
    health_enabled: draft.value.health_enabled,
    notify_enabled: draft.value.notify_enabled,
    health_interval_s: blankToNull(draft.value.health_interval_s),
    health_fail_threshold: blankToNull(draft.value.health_fail_threshold),
    tags: draft.value.tags,
  }
  try {
    const saved = await withWriteGate(() =>
      api.patchService(props.service.slug, body),
    )
    if (!saved) return
    upsertLocal(saved)
    editing.value = false
    emit('changed')
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

async function remove() {
  if (!window.confirm(`Delete ${props.service.name}? This cannot be undone.`)) return
  busy.value = true
  try {
    const done = await withWriteGate(() => api.deleteService(props.service.slug))
    if (!done) return
    removeLocal(props.service.slug)
    emit('back')
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

function initials(name) {
  return (name || '?').replace(/[^A-Za-z0-9 ]/g, '').slice(0, 2).toUpperCase()
}

if (props.startEditing) startEdit()
</script>

<template>
  <div class="content">
    <button class="link-btn" style="margin-bottom: 16px" @click="emit('back')">
      ← Catalog
    </button>

    <div class="page-head">
      <div class="record-head">
        <!-- The icon is the obvious place to look for icon controls, so it is
             the way in to them rather than something you find inside Edit. -->
        <button
          class="record-icon"
          :title="service.icon_url ? 'Change icon' : 'Add an icon'"
          @click="startEdit"
        >
          <img v-if="service.icon_url" :src="service.icon_url" alt="" />
          <template v-else>{{ initials(service.name) }}</template>
          <span class="record-icon-hint">✎</span>
        </button>
        <div>
          <div style="display: flex; align-items: center; gap: 10px">
            <span class="page-title" style="font-size: 20px">{{ service.name }}</span>
            <StatusPill :service="service" :note="false" />
          </div>
          <div class="record-url">{{ service.launch_url }}</div>
        </div>
      </div>
      <!-- A class, not the inline style it used to be: the phone layout has to
           re-flow this row, and media queries cannot override inline styles. -->
      <div class="record-actions">
        <button
          class="btn-ghost"
          :disabled="rechecking || !service.health_enabled"
          :title="service.health_enabled ? 'Check this service now' : 'Health checks are disabled'"
          @click="recheck"
        >
          {{ rechecking ? 'Checking…' : '↻ Recheck' }}
        </button>
        <button class="btn-ghost" @click="startEdit">Edit</button>
        <button class="btn-ghost btn-danger" :disabled="busy" @click="remove">Delete</button>
        <button class="btn-primary" @click="launch(service)">Open ↗</button>
      </div>
    </div>

    <div v-if="error" class="notice err" style="margin-top: 16px">{{ error }}</div>

    <!-- Edit form -->
    <div v-if="editing" class="card" style="padding: 20px 22px">
      <div class="field-row">
        <div class="field">
          <label class="field-label">Display name</label>
          <input v-model="draft.name" />
        </div>
        <div class="field">
          <label class="field-label">Category</label>
          <!-- This field had no suggestions at all until 2026-08-07: the add
               form offered autocomplete and the edit form silently did not, so
               editing a service was the easiest way to invent a duplicate
               category. Same component both places now. -->
          <ComboInput
            v-model="draft.category"
            :suggestions="allCategories.map((c) => c.name)"
            placeholder="Media"
          />
        </div>
      </div>
      <div class="field">
        <label class="field-label">Launch URL</label>
        <input v-model="draft.launch_url" class="mono" />
      </div>
      <div class="field">
        <label class="field-label">Health URL</label>
        <input v-model="draft.health_url" class="mono" placeholder="same as Launch URL" />
      </div>
      <div class="field-row">
        <div class="field">
          <label class="field-label">Location</label>
          <input v-model="draft.host_label" :placeholder="derived || 'not set'" />
          <!-- §7b requires the known limits be stated rather than discovered.
               They are not edge cases here: on a typical cluster nearly half the
               catalog sits on a couple of Docker hosts, so "the host, not this
               service" is what most derived values mean. -->
          <div class="field-hint">
            <template v-if="derived">
              Leave empty to follow Proxmox, which reports
              <strong>{{ derived }}</strong>. Typing a value pins it and stops it
              tracking.
            </template>
            <template v-else>
              Typing a value pins it. Left empty, it follows Proxmox when a guest
              matches this service's address.
            </template>
            Containers running Docker resolve to their <em>host</em>, not to
            themselves; VMs need the QEMU guest agent to report an address.
          </div>
        </div>
        <div class="field">
          <label class="field-label">Tags</label>
          <TagInput
            v-model="draft.tags"
            :suggestions="allTags"
          />
        </div>
      </div>
      <div class="field">
        <label class="field-label">Description</label>
        <textarea v-model="draft.description" rows="2" />
      </div>
      <IconPicker
        v-model="draft.icon_url"
        :fallback="draft.name"
        :slug="service.slug"
      />
      <div class="field-row">
        <div class="field">
          <label class="field-label">Poll interval</label>
          <input
            v-model="draft.health_interval_s"
            type="number"
            :min="15"
            :placeholder="`follows Settings (${service.health_interval_effective_s}s)`"
          />
          <div class="field-hint">
            Leave empty to follow Settings. Minimum 15 seconds.
          </div>
        </div>
        <div class="field">
          <label class="field-label">Fail threshold</label>
          <input
            v-model="draft.health_fail_threshold"
            type="number"
            :min="1"
            placeholder="follows Settings"
          />
          <div class="field-hint">Failures in a row before this shows as down.</div>
        </div>
      </div>
      <div class="field">
        <label style="font: 500 12.5px 'Manrope', sans-serif; color: var(--txt2)">
          <input v-model="draft.health_enabled" type="checkbox" style="width: auto; margin-right: 8px" />
          Health checks enabled
        </label>
        <!-- Separate from health checks on purpose (§4): loud on the dashboard
             without waking you. Turning this off changes nothing about the
             status shown here. -->
        <label
          style="font: 500 12.5px 'Manrope', sans-serif; color: var(--txt2); display: block; margin-top: 10px"
        >
          <input v-model="draft.notify_enabled" type="checkbox" style="width: auto; margin-right: 8px" />
          Notify on status changes
        </label>
      </div>
      <div style="display: flex; gap: 10px">
        <span style="margin-left: auto" />
        <button class="btn-ghost" @click="editing = false">Cancel</button>
        <button class="btn-primary" :disabled="busy" @click="save">Save</button>
      </div>
    </div>

    <!-- Record -->
    <div v-else class="card" style="overflow: hidden">
      <div class="meta-grid">
        <div class="meta-k">Slug</div>
        <div class="meta-v">{{ service.slug }}</div>
        <div class="meta-k">Category</div>
        <div class="meta-v">{{ service.category || '—' }}</div>
        <div class="meta-k">Tags</div>
        <div class="meta-v">{{ (service.tags || []).join(', ') || '—' }}</div>
        <div class="meta-k">Location</div>
        <div class="meta-v">
          <!-- Pinned wins on display (§4a). Derived is shown beside it, never
               instead of it, and never merged into it. -->
          <template v-if="pinned">
            {{ pinned }}
            <span class="loc-tag">pinned</span>
          </template>
          <template v-else-if="derived">
            {{ derived }}
            <span class="loc-tag">from {{ service.location_provider }}</span>
            <!-- Audit S8, the UI half: the provider has missed this guest for
                 several polls. The value is still served — stale-and-said
                 beats silently current-looking — and if the guest stays
                 missing the link is dropped entirely. -->
            <span
              v-if="service.location_stale"
              class="loc-tag warn"
              title="Proxmox has not reported this guest for several polls. The location may be out of date; if the guest stays missing the link is dropped."
            >stale</span>
          </template>
          <template v-else>—</template>

          <div v-if="disagrees" class="loc-disagree">
            <span class="loc-disagree-text">
              {{ service.location_provider }} reports
              <strong>{{ derived }}</strong>
            </span>
            <button class="link-btn" :disabled="busy" @click="unpin">
              Unpin and follow it
            </button>
          </div>
        </div>
        <div class="meta-k">Description</div>
        <div class="meta-v">{{ service.description || '—' }}</div>
        <div class="meta-k">Launch URL</div>
        <div class="meta-v">{{ service.launch_url }}</div>
        <div class="meta-k">External URL</div>
        <div class="meta-v">{{ service.launch_url_external || '—' }}</div>
        <div class="meta-k">Health URL</div>
        <div class="meta-v">{{ service.health_url || 'same as launch URL' }}</div>
        <div class="meta-k">Source</div>
        <div class="meta-v">{{ service.provenance }}{{ service.created_by ? ` · ${service.created_by}` : '' }}</div>
      </div>

      <div class="section-title">Health</div>
      <div class="meta-grid" style="margin-top: 10px">
        <div class="meta-k">Status</div>
        <div class="meta-v">{{ service.status }} (stored: {{ service.health_status }})</div>
        <div class="meta-k">Checks</div>
        <div class="meta-v">{{ service.health_enabled ? 'enabled' : 'disabled' }}</div>
        <div class="meta-k">Notifications</div>
        <div class="meta-v">
          {{ service.notify_enabled ? 'on' : 'off — checked, never announced' }}
        </div>
        <div class="meta-k">Interval</div>
        <div class="meta-v">
          {{ service.health_interval_effective_s }}s{{ service.health_interval_s === null ? ' (follows Settings)' : ' (pinned)' }}
          · timeout {{ asSeconds(service.health_timeout_ms) }}
          · threshold {{ service.health_fail_threshold === null ? 'follows Settings' : service.health_fail_threshold }}
        </div>
        <div class="meta-k">Last checked</div>
        <div class="meta-v">{{ service.health_checked_at || 'never' }}</div>
        <div class="meta-k">Changed</div>
        <div class="meta-v">{{ service.health_changed_at || '—' }}</div>
        <div class="meta-k">Consecutive fails</div>
        <div class="meta-v">{{ service.health_consec_failures }}</div>
      </div>
    </div>
  </div>
</template>
