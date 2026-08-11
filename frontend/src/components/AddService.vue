<script setup>
/**
 * Add service — spec §7.
 *
 * Fast path is the default door and targets under 15 seconds:
 *   paste URL → probe → autofill slug/name/icon/category → Save
 * One required field: the URL. The display name is collected ONCE (the design
 * collected it on both step 0 and step 2; §7 calls that out as a fix).
 *
 * Bulk import (§7, §6b) takes one URL per line plus a single tag applied to the
 * whole batch — without that, tag-based saved views appear broken on day one.
 */
import { computed, ref } from 'vue'

import { api } from '../api.js'
import ComboInput from './ComboInput.vue'
import {
  allCategories,
  allTags,
  state,
  upsertLocal,
  withWriteGate,
} from '../store.js'
import BaseModal from './BaseModal.vue'
import IconPicker from './IconPicker.vue'
import TagInput from './TagInput.vue'

const emit = defineEmits(['close', 'saved'])

const mode = ref('fast') // fast | full | bulk

const url = ref('')
const name = ref('')
const slug = ref('')
const category = ref('')
const hostLabel = ref('')
const tags = ref([])
const description = ref('')
const healthUrl = ref('')
const healthEnabled = ref(true)

const slugTouched = ref(false)
const probing = ref(false)
const probed = ref(null)
/** Local media path (or inline data URI) of the icon, once resolved. */
const iconUrl = ref('')
const probeError = ref('')
const busy = ref(false)
const error = ref('')

const bulkText = ref('')
const bulkTag = ref([])
const bulkCategory = ref('')
const bulkResults = ref([])
/** 'input' -> paste URLs, 'review' -> approve derived slugs, then import. */
const bulkStage = ref('input')
const bulkRows = ref([])

const catalogSlugs = computed(
  () => new Set(state.services.map((s) => s.slug)),
)

/** Slugs claimed more than once within this batch. */
const duplicateSlugs = computed(() => {
  const seen = new Map()
  for (const row of bulkRows.value) {
    if (!row.slug) continue
    seen.set(row.slug, (seen.get(row.slug) || 0) + 1)
  }
  return new Set([...seen.entries()].filter(([, n]) => n > 1).map(([slug]) => slug))
})

function rowWarning(row) {
  if (!row.slug) return 'Needs a slug'
  if (duplicateSlugs.value.has(row.slug)) {
    // POST is upsert-on-slug, so without this the later row silently replaces
    // the earlier one and a service disappears from the import.
    return 'Duplicate slug in this batch — only the last one would survive'
  }
  if (catalogSlugs.value.has(row.slug)) return 'Already in the catalog — will update it'
  return ''
}

const bulkBlocked = computed(() =>
  bulkRows.value.some((r) => !r.slug || duplicateSlugs.value.has(r.slug)),
)

async function previewBulk() {
  const urls = bulkText.value
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
  if (!urls.length) return

  error.value = ''
  busy.value = true
  bulkStage.value = 'review'
  // `id`, not the URL, is the row's identity: the same URL pasted twice is two
  // rows, and keying Vue (or the post-import cleanup) on `row.url` made them
  // one — duplicate keys, and both copies dropped when either landed (audit
  // informational).
  bulkRows.value = urls.map((u, index) => ({
    id: index,
    url: u,
    slug: '',
    name: '',
    ok: null,
    status: null,
    probing: true,
  }))

  for (const [index, one] of urls.entries()) {
    try {
      const probe = await withWriteGate(() => api.probe(one))
      if (!probe) {
        // Cancelled at the gate. Fall back to URL-derived slugs so the batch is
        // still reviewable without probing.
        for (let rest = index; rest < urls.length; rest += 1) {
          const preview = await api.slugPreview(urls[rest]).catch(() => null)
          bulkRows.value[rest] = {
            id: bulkRows.value[rest].id,
            url: urls[rest],
            slug: preview?.slug || '',
            name: '',
            ok: null,
            status: null,
            probing: false,
            unprobed: true,
          }
        }
        break
      }
      bulkRows.value[index] = {
        id: bulkRows.value[index].id,
        url: one,
        slug: probe.suggested_slug || '',
        name: probe.suggested_name || '',
        ok: probe.ok,
        status: probe.status,
        error: probe.error,
        probing: false,
      }
    } catch (err) {
      bulkRows.value[index] = {
        id: bulkRows.value[index].id,
        url: one,
        slug: '',
        name: '',
        ok: false,
        probing: false,
        error: err.message,
      }
    }
  }
  busy.value = false
}

function removeBulkRow(index) {
  bulkRows.value.splice(index, 1)
}


// A slug cannot be derived from an IP address, so when the probe also finds
// nothing there is genuinely no identity available and the user has to supply
// one. Saving is blocked rather than inventing a placeholder, because the slug
// is immutable once written.
const canSave = computed(
  () => url.value.trim().length > 0 && slug.value.trim().length > 0,
)
const needsIdentity = computed(
  () => url.value.trim().length > 0 && !slug.value.trim() && !probing.value,
)
/** Last URL actually sent to /api/probe, so blur+Enter don't fire twice. */
const probedUrl = ref('')

async function runProbe(force = false) {
  const target = url.value.trim()
  if (!target) return
  // The URL field blurs when the write gate opens over it, which would start a
  // second probe, park it, and orphan the first one. Guard on both.
  if (probing.value) return
  if (!force && target === probedUrl.value) return

  probedUrl.value = target
  probing.value = true
  probeError.value = ''
  probed.value = null
  try {
    // The probe is admin-scope only (§4c), so this is the first thing that can
    // trip the write gate.
    const result = await withWriteGate(() => api.probe(target))
    if (!result) {
      // Cancelled at the gate — allow a retry of the same URL.
      probedUrl.value = ''
      probeError.value =
        'Probing needs the admin token. You can still save without probing.'
      return
    }
    probed.value = result
    // Only adopt the suggestion if the field is untouched — never overwrite a
    // slug the user has deliberately typed.
    if (!slugTouched.value) slug.value = result.suggested_slug || ''
    if (!name.value && result.suggested_name) name.value = result.suggested_name
    if (result.icon_url && !iconUrl.value) iconUrl.value = result.icon_url
    if (!result.ok && result.error) probeError.value = result.error
  } catch (err) {
    probeError.value = err.message || 'Probe failed.'
  } finally {
    probing.value = false
  }
}

function payload() {
  const body = { launch_url: url.value.trim() }
  // Sent as `slug`; the server normalises it through the same derivation the
  // UI would use, so the two cannot diverge.
  if (slug.value.trim()) body.slug = slug.value.trim()
  if (name.value.trim()) body.name = name.value.trim()
  if (category.value.trim()) body.category = category.value.trim()
  if (hostLabel.value.trim()) body.host_label = hostLabel.value.trim()
  if (description.value.trim()) body.description = description.value.trim()
  if (healthUrl.value.trim()) body.health_url = healthUrl.value.trim()
  if (!healthEnabled.value) body.health_enabled = false
  if (iconUrl.value) body.icon_url = iconUrl.value
  if (tags.value.length) body.tags = tags.value
  return body
}

async function save() {
  busy.value = true
  error.value = ''
  try {
    const saved = await withWriteGate(() => api.upsertService(payload()))
    if (!saved) return // gate opened; the pending write replays after unlock
    upsertLocal(saved)
    emit('saved', `Saved ${saved.name}.`)
    emit('close')
  } catch (err) {
    error.value = err.message || 'Could not save.'
  } finally {
    busy.value = false
  }
}

async function runBulk() {
  const rows = bulkRows.value.filter((r) => r.slug)
  if (!rows.length) return

  busy.value = true
  error.value = ''
  bulkResults.value = rows.map((r) => ({ id: r.id, url: r.url, state: 'pending' }))

  const batchTags = bulkTag.value
  const batchCategory = bulkCategory.value.trim()

  for (const [index, row] of rows.entries()) {
    const body = { launch_url: row.url, slug: row.slug }
    if (row.name) body.name = row.name
    if (batchTags.length) body.tags = batchTags
    // A supplied category also suppresses the server's keyword guess, which is
    // what you want when you already know what this batch is.
    if (batchCategory) body.category = batchCategory
    try {
      const saved = await withWriteGate(() => api.upsertService(body))
      if (!saved) {
        // Cancelled at the write gate — say what was not attempted rather than
        // leaving the rest of the list looking pending forever.
        bulkResults.value[index] = { id: row.id, url: row.url, state: 'locked' }
        for (let rest = index + 1; rest < rows.length; rest += 1) {
          bulkResults.value[rest] = { id: rows[rest].id, url: rows[rest].url, state: 'skipped' }
        }
        break
      }
      upsertLocal(saved)
      bulkResults.value[index] = { id: row.id, url: row.url, state: 'ok', slug: saved.slug }
    } catch (err) {
      bulkResults.value[index] = { id: row.id, url: row.url, state: 'fail', error: err.message }
    }
  }
  busy.value = false

  const imported = bulkResults.value.filter((r) => r.state === 'ok')
  emit('saved', `Imported ${imported.length} service(s).`)

  // Drop the rows that landed. Anything still listed is a row that needs
  // attention — leaving them all up would re-flag the just-created services as
  // "already in the catalog", which reads like a warning about your own import.
  // By id: dropping by URL took a failed duplicate down with its successful
  // twin, hiding exactly the row that needed attention.
  const done = new Set(imported.map((r) => r.id))
  bulkRows.value = bulkRows.value.filter((row) => !done.has(row.id))

  if (!bulkRows.value.length) emit('close')
}
</script>

<template>
  <BaseModal title="Add service" wide @close="emit('close')">
    <div class="modal-body">

      <div class="seg" style="width: fit-content; margin-bottom: 18px">
        <button :class="{ on: mode === 'fast' }" @click="mode = 'fast'">Fast path</button>
        <button :class="{ on: mode === 'full' }" @click="mode = 'full'">More options</button>
        <button :class="{ on: mode === 'bulk' }" @click="mode = 'bulk'">Bulk import</button>
      </div>

      <div v-if="error" class="notice err">{{ error }}</div>

      <!-- Fast path and full path share the same fields; "more options" simply
           reveals the rest. The display name is collected exactly once. -->
      <template v-if="mode !== 'bulk'">
        <div class="field">
          <label class="field-label" for="launch-url">Launch URL — the only required field</label>
          <input
            id="launch-url"
            v-model="url"
            class="mono"
            placeholder="https://karakeep.lab.local"
            @blur="runProbe(false)"
            @keydown.enter="runProbe(true)"
          />
          <div class="field-hint">
            Paste and press Enter to probe. A service with only a name and URL is
            a complete, valid record.
          </div>
        </div>

        <div v-if="probing" class="spinner" style="margin-bottom: 14px">Probing…</div>
        <div v-if="probeError" class="notice warn">{{ probeError }}</div>

        <div v-if="probed" class="probe-rows">
          <div class="probe-row">
            <span class="k">Reachable</span>
            <span class="v">{{ probed.ok ? `yes · HTTP ${probed.status}` : 'no' }}</span>
          </div>
          <div v-if="probed.title" class="probe-row">
            <span class="k">Title</span><span class="v">{{ probed.title }}</span>
          </div>
          <div v-if="probed.final_url" class="probe-row">
            <span class="k">Final URL</span><span class="v">{{ probed.final_url }}</span>
          </div>
          <!-- Shown as a FACT about the probed target, alongside Title and
               Final URL — never written into the Location input below. That
               input is the *pinned* value, so pre-filling it would pin every
               service at creation and the derived value could never track
               again (§4a). This row exists so the empty field stops reading as
               broken. -->
          <div v-if="probed.derived_location" class="probe-row">
            <span class="k">Location</span>
            <span class="v">
              {{ probed.derived_location }}
              <span class="probe-note">
                from {{ probed.derived_location_provider }} · leave Location
                empty to follow it
              </span>
            </span>
          </div>
        </div>

        <div class="field">
          <label class="field-label" for="svc-name">Display name</label>
          <input id="svc-name" v-model="name" placeholder="Derived from the page title" />
        </div>

        <div class="field">
          <label class="field-label" for="svc-slug">Slug · permanent identifier</label>
          <input
            id="svc-slug"
            v-model="slug"
            class="mono"
            :class="{ 'needs-value': needsIdentity }"
            :placeholder="needsIdentity ? 'required — e.g. immich' : 'derived from the URL'"
            @input="slugTouched = true"
          />
          <div v-if="needsIdentity" class="field-hint" style="color: var(--warn)">
            This URL is an IP address, so there is no name to derive from — and
            the probe found nothing either. Give the service a slug and a display
            name.
          </div>
          <div v-else class="field-hint">
            The service's permanent name — its API path and the key that decides
            whether a re-registration updates this record or creates a second
            one. <strong>It cannot be changed after saving</strong>, so prefer
            what the service <em>is</em> over where it currently lives.
          </div>
        </div>

        <div class="field">
          <label class="field-label" for="svc-cat">Category</label>
          <ComboInput
            id="svc-cat"
            v-model="category"
            :suggestions="allCategories.map((c) => c.name)"
            placeholder="Media"
            empty-hint="No categories yet — the first one you type becomes a suggestion."
          />
          <!-- The placeholder is Title Case on purpose: category is free text
               with no case normalisation, so a lowercase example invites a
               second `media` alongside an existing `Media`.

               One word of hint, not twenty. The combobox opens its list on
               focus, so it now demonstrates that suggestions exist rather than
               needing a sentence to claim it — which is the better pattern and
               happens to remove 20 words from the densest form in the app. -->
          <div class="field-hint">Optional.</div>
        </div>

        <IconPicker v-model="iconUrl" :fallback="name" />

        <template v-if="mode === 'full'">
          <div class="field">
            <label class="field-label" for="svc-host">Location · optional</label>
            <!-- The placeholder was the static example `pve-c · CT 117`, which
                 looks exactly like a real derived location and so read as "about
                 to be filled in for you". It is not: this field PINS. -->
            <input
              id="svc-host"
              v-model="hostLabel"
              :placeholder="
                probed && probed.derived_location
                  ? `empty = ${probed.derived_location}`
                  : 'leave empty to follow Proxmox'
              "
            />
            <div class="field-hint">
              <template v-if="probed && probed.derived_location">
                Leave empty and this follows Proxmox, including when the guest
                moves node. Typing a value <strong>pins</strong> it and stops it
                tracking.
              </template>
              <template v-else>
                Leave empty to follow Proxmox when a guest matches this address.
                Typing a value <strong>pins</strong> it and stops it tracking.
              </template>
            </div>
          </div>
          <div class="field">
            <label class="field-label">Tags</label>
            <TagInput
              v-model="tags"
              :suggestions="allTags"
              placeholder="Type a tag, press Enter…"
            />
          </div>
          <div class="field">
            <label class="field-label" for="svc-desc">Description</label>
            <textarea id="svc-desc" v-model="description" rows="2" />
          </div>
          <div class="field">
            <label class="field-label" for="svc-health">Health URL</label>
            <input
              id="svc-health"
              v-model="healthUrl"
              class="mono"
              placeholder="same as Launch URL"
            />
            <div class="field-hint">
              A complete probe URL, not a base. The browser may need
              <code>https://name.lab.local</code> while the worker needs
              <code>http://192.168.50.101:3000</code>.
            </div>
          </div>
          <div class="field">
            <label class="field-label">Health checks</label>
            <label style="font: 500 12.5px 'Manrope', sans-serif; color: var(--txt2)">
              <input
                v-model="healthEnabled"
                type="checkbox"
                style="width: auto; margin-right: 8px"
              />
              Check this service. Off means it still lists and launches — it just
              shows no status.
            </label>
          </div>
        </template>
      </template>

      <!-- Bulk backfill (§7) -->
      <template v-else>
        <div class="field-hint" style="margin-bottom: 16px">
          Everything in one batch gets the same category and tags, so import
          things that belong together — one batch per category is the easy way
          to work. Both are editable per service afterwards.
        </div>

        <div class="field-row">
          <div class="field">
            <label class="field-label" for="bulk-cat">Category for this whole batch</label>
            <ComboInput
              id="bulk-cat"
              v-model="bulkCategory"
              :suggestions="allCategories.map((c) => c.name)"
              placeholder="Media"
            />
            <div class="field-hint">
              Drives the sidebar grouping. Left blank, each service gets a
              keyword guess instead.
            </div>
          </div>

          <div class="field">
            <label class="field-label">Tags for this whole batch</label>
            <TagInput
              v-model="bulkTag"
              :suggestions="allTags"
              placeholder="Type a tag, press Enter…"
            />
            <div class="field-hint">
              Saved views are stored queries, and the useful ones depend on tags
              existing. Tagging the batch is what makes them work immediately.
            </div>
          </div>
        </div>
        <div v-if="bulkStage === 'input'" class="field">
          <label class="field-label" for="bulk-urls">One URL per line</label>
          <textarea id="bulk-urls" v-model="bulkText" rows="8" placeholder="https://nas.lan:5001&#10;https://udm.lan&#10;https://pihole.lab.local" />
        </div>

        <!-- Review: approve the derived slug before anything is written. The
             slug is immutable after creation and POST upserts on it, so this is
             the last point at which a mistake is cheap. -->
        <template v-else>
          <div v-if="bulkBlocked" class="notice warn">
            Resolve the highlighted rows before importing — a duplicate slug
            means only the last of them would survive.
          </div>

          <div class="bulk-review">
            <div class="bulk-head">
              <span>URL</span><span>Slug · permanent</span><span>Name</span><span></span>
            </div>
            <div v-for="(row, index) in bulkRows" :key="row.id" class="bulk-row">
              <div class="bulk-url" :title="row.url">
                <span v-if="row.probing" class="spinner">probing…</span>
                <template v-else>
                  <span
                    class="bulk-dot"
                    :style="{
                      background: row.unprobed
                        ? 'var(--txt3)'
                        : row.ok
                          ? 'var(--ok)'
                          : 'var(--err)',
                    }"
                  />
                  {{ row.url }}
                </template>
              </div>
              <!-- The placeholders/labels repeat the head row's column names
                   because the phone layout hides that row (styles.css,
                   responsive) — without them the two inputs are anonymous. -->
              <input
                v-model="row.slug"
                class="mono bulk-input"
                :class="{ bad: rowWarning(row) && !catalogSlugs.has(row.slug) }"
                :disabled="row.probing"
                placeholder="slug"
                aria-label="Slug"
              />
              <input
                v-model="row.name"
                class="bulk-input"
                :disabled="row.probing"
                placeholder="name"
                aria-label="Name"
              />
              <button
                class="clear-btn"
                title="Drop this row"
                @click="removeBulkRow(index)"
              >
                ✕
              </button>
              <div v-if="rowWarning(row)" class="bulk-warn">{{ rowWarning(row) }}</div>
            </div>
          </div>
        </template>

        <div v-if="bulkResults.length" class="probe-rows" style="margin-top: 14px">
          <div v-for="r in bulkResults" :key="r.id" class="probe-row">
            <span class="k">
              {{ r.state === 'ok' ? 'added' : r.state === 'pending' ? '…' : r.state }}
            </span>
            <span class="v">{{ r.url }}</span>
          </div>
        </div>
      </template>
    </div>

    <div class="modal-foot">
      <span class="spacer" />
      <button class="btn-ghost" @click="emit('close')">Cancel</button>
      <button
        v-if="mode !== 'bulk'"
        class="btn-primary"
        :disabled="!canSave || busy"
        @click="save"
      >
        {{ busy ? 'Saving…' : 'Save service' }}
      </button>
      <template v-else>
        <button
          v-if="bulkStage === 'input'"
          class="btn-primary"
          :disabled="busy || !bulkText.trim()"
          @click="previewBulk"
        >
          Probe and review →
        </button>
        <template v-else>
          <button class="btn-ghost" :disabled="busy" @click="bulkStage = 'input'">
            ← Back
          </button>
          <button
            class="btn-primary"
            :disabled="busy || bulkBlocked || !bulkRows.length"
            @click="runBulk"
          >
            {{ busy ? 'Importing…' : `Import ${bulkRows.length} service(s)` }}
          </button>
        </template>
      </template>
    </div>
  </BaseModal>
</template>
