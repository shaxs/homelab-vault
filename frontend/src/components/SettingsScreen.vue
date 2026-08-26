<script setup>
/**
 * Settings — v1 §6a, §6c. Follows `design/HomeLab OS Settings v1.html`.
 *
 * The screen's organising idea comes from the mockup and is worth stating,
 * because it is what makes §6a's "what lives where" visible rather than
 * documented: **editable preferences sit on ordinary rows with real controls;
 * anything owned by `.env` sits in a recessed block with its variable name, a
 * status instead of a value, and no affordance at all.** An operator can see
 * which half of the configuration they are looking at without being told.
 *
 * Rows whose consumer has not shipped yet render *inert with the reason*, which
 * is the same pattern the mockup uses for an unconfigured Proxmox — honest
 * about the state of the system rather than a control that silently does
 * nothing.
 */
import { computed, onMounted, ref } from 'vue'

import { api } from '../api.js'
import { navigate } from '../router.js'
import { loadPublicSettings, state, withWriteGate } from '../store.js'
import { useTheme } from '../theme.js'

const props = defineProps({
  /** The section route parameter — a slug like `health-checks`. Empty on the
   *  bare `/settings` route, which shows the first section. */
  section: { type: String, default: '' },
})
const emit = defineEmits(['back'])

const theme = useTheme()
const loading = ref(true)
const error = ref('')
const saving = ref(false)
const saved = ref({})
const draft = ref({})
const specs = ref([])
const environment = ref([])
const providers = ref([])
const sections = ref([])
const authMode = ref('full')

const AUTH_MODES = [
  {
    key: 'full',
    desc: 'Token required for all writes. The default, and the only mode safe on a network you do not control.',
  },
  {
    key: 'open-writes',
    desc: 'No token to add or edit services. Destructive bulk operations still require the admin token.',
  },
  {
    key: 'open',
    desc: 'No token for anything except the probe and icon endpoints, which are always admin-only.',
  },
]

/** Which card a section's `.env` rows belong under. */
const ENV_SECTION = {
  HOMELABVAULT_DISCORD_WEBHOOK_URL: 'Notifications',
  PROXMOX_HOST: 'Proxmox',
  PROXMOX_API_TOKEN: 'Proxmox',
  // TN2: without these, the backend's TRUENAS_* .env rows fall through to the
  // Access card instead of the TrueNAS one — the .env-panel half of the
  // settings producer/consumer pair. (The Proxmox-style "Connected" status
  // aside for the TrueNAS card is TN4's richer frontend pass.)
  TRUENAS_HOST: 'TrueNAS',
  TRUENAS_API_KEY: 'TrueNAS',
  // TN-WS: 25.04 keys are user-linked, so the username is a third credential —
  // it belongs beside the key it unlocks, and its "Not configured" pill is how
  // an operator sees the missing third piece.
  TRUENAS_API_USER: 'TrueNAS',
}

const specsBySection = computed(() => {
  const grouped = {}
  for (const spec of specs.value) {
    ;(grouped[spec.section] ||= []).push(spec)
  }
  return grouped
})

const envBySection = computed(() => {
  const grouped = { Access: [] }
  for (const row of environment.value) {
    const section = ENV_SECTION[row.env_var] || 'Access'
    ;(grouped[section] ||= []).push(row)
  }
  return grouped
})

/** Proxmox is configured in .env, not here (§6a). V1.4 wires the worker. */
const proxmoxConfigured = computed(() =>
  environment.value.some(
    (row) => row.env_var === 'PROXMOX_HOST' && row.status !== 'Not configured',
  ),
)

/**
 * Live provider health, served from the worker's cached snapshot (§7c) — this
 * screen never calls Proxmox itself.
 *
 * **`configured` is not the same as working**, and rendering only the former
 * would put a green tick over a provider that can see nothing. Proxmox filters
 * collections by privilege and answers `200 []` rather than 403, so a token
 * with no ACL enumerates zero guests through an entirely healthy-looking call.
 * `usable` is the field that tells those apart, and `detail` carries the fix.
 */
const proxmoxStatus = computed(
  () => providers.value.find((p) => p.name === 'proxmox') || null,
)

const changed = computed(() =>
  specs.value
    .filter((spec) => draft.value[spec.key] !== saved.value[spec.key])
    .map((spec) => spec),
)

const dirty = computed(() => changed.value.length > 0)

// --- Section navigation (TN-SET) ------------------------------------------
//
// One section shows at a time, picked by the `/settings/{section}` route. The
// rail is DERIVED from the backend's section list (plus the two frontend-owned
// cards), so a future provider's section gets a rail entry and a route with no
// frontend change — the same property the adapter bar has.

/** The label of the links card, which is frontend-owned (not a backend section). */
const LINKS_SECTION = 'Data and API'

/** `Health checks` -> `health-checks`. Stable, readable slugs derived from the
 *  labels rather than a second hand-maintained list that could drift. */
function slugFor(label) {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, '-')
}

const railItems = computed(() =>
  [...sections.value, LINKS_SECTION].map((label) => ({ label, slug: slugFor(label) })),
)

/**
 * The section being shown. An unknown or absent slug degrades to the first
 * section rather than a dead page — same idiom as an unknown dashboard
 * adapter degrading to Overview.
 */
const activeSection = computed(() => {
  const found = railItems.value.find((item) => item.slug === props.section)
  return found?.label || railItems.value[0]?.label || ''
})

/** Sections with unsaved edits, so the rail can show where they live even
 *  while a different section is on screen. */
const dirtySections = computed(() => {
  const marked = new Set()
  for (const spec of changed.value) marked.add(spec.section)
  return marked
})

function goSection(item) {
  navigate('settings', { section: item.slug })
}

/** The backend sections the body renders — just the active one. The card
 *  markup inside the loop is untouched by TN-SET; only the loop's source
 *  narrowed, which is what keeps every card-level test meaningful. */
const visibleSections = computed(() =>
  sections.value.filter((section) => section === activeSection.value),
)

/** Section badge: are its rows editable here, or owned by .env, or both? */
/**
 * The card's badge, with the explanation on hover.
 *
 * "Mixed" was the one nobody could decode without reading the code — it means
 * the card mixes values you can change here with values that come from `.env`,
 * which is §6a's "what lives where" made visible. A one-word badge cannot carry
 * that, so it carries a title instead.
 */
function badgeFor(section) {
  const hasEditable = (specsBySection.value[section] || []).length > 0
  const hasEnv = (envBySection.value[section] || []).length > 0
  if (hasEditable && hasEnv) {
    return {
      label: 'Mixed',
      tone: 'warn',
      hint: 'Some values on this card are editable here; the rest come from .env and need a restart to change.',
    }
  }
  if (hasEditable) {
    return {
      label: 'Editable',
      tone: 'accent',
      hint: 'Every value on this card can be changed here and takes effect without a restart.',
    }
  }
  return {
    label: 'Read-only',
    tone: 'muted',
    hint: 'Every value on this card comes from .env and needs a restart to change.',
  }
}

/**
 * Derived notification copy — §5a's "derived, re-read live, never snapshotted"
 * applied to the words as much as to the evaluator.
 *
 * Every number below comes from `draft`, so the screen describes the values the
 * operator is looking at rather than the ones that happened to be loaded. A
 * hardcoded "after 3 minutes" would go quietly wrong the first time somebody
 * changes the interval, which is the failure §5a exists to prevent.
 */
const stallAfterS = computed(
  () => Math.max(Number(draft.value['health.default_interval_s']) || 60, 1) * 3,
)

function humanSeconds(seconds) {
  if (seconds % 3600 === 0) {
    const hours = seconds / 3600
    return `${hours} hour${hours === 1 ? '' : 's'}`
  }
  if (seconds % 60 === 0) {
    const minutes = seconds / 60
    return `${minutes} minute${minutes === 1 ? '' : 's'}`
  }
  return `${seconds} seconds`
}

function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? '' : 's'}`
}

const notificationTriggers = computed(() => {
  const fail = Math.max(Number(draft.value['health.fail_threshold']) || 1, 1)
  const recovery = Math.max(Number(draft.value['health.recovery_threshold']) || 1, 1)
  return [
    `A service fails ${plural(fail, 'check')} in a row — it is announced as down.`,
    `That service then passes ${plural(recovery, 'check')} in a row — it is announced as recovered. Fewer would post on every bounce of a flapping service.`,
    `A background worker stops reporting for ${humanSeconds(stallAfterS.value)} — the dashboard would otherwise freeze at its last values and say nothing.`,
  ]
})

const SECTION_ASIDE = {
  Appearance: 'Theme is per-device · landing page is server-wide',
  Panels: 'Dashboard composition',
  'Health checks': 'Applies to every service without its own interval',
  Notifications: 'Toggle here, credential in .env',
  Access: 'Every value on this card comes from .env',
}

function asideFor(section) {
  if (section === 'Proxmox') {
    return proxmoxConfigured.value ? 'Connected' : 'Not configured'
  }
  return SECTION_ASIDE[section] || ''
}

/**
 * A row is inert when the thing it configures cannot act yet.
 *
 * Two causes, and the copy has to say which: Proxmox is not configured in
 * `.env`, or the consumer ships in a later step. Rendering a live-looking
 * control for either is how a settings screen starts lying.
 */
function inertReason(spec) {
  if (spec.section === 'Proxmox' && !proxmoxConfigured.value) {
    return 'Set PROXMOX_HOST and PROXMOX_API_TOKEN in .env, then restart the server. Until then the poller stays off and Proxmox panels are hidden from every dashboard. The saved preference is kept and reapplies once Proxmox is configured.'
  }
  if (spec.key.startsWith('ui.panels.proxmox') && !proxmoxConfigured.value) {
    return 'Unavailable until Proxmox is configured in .env. The saved preference is kept and reapplies.'
  }
  return ''
}

/** Options a segmented control may actually offer today. */
function optionsFor(spec) {
  return spec.choices
}

const LANDING_LABELS = { catalog: 'Service Catalog', dashboard: 'Command Center' }
const DENSITY_LABELS = { compact: 'Compact', comfortable: 'Comfortable' }

function optionLabel(spec, choice) {
  if (spec.key === 'ui.landing_page') return LANDING_LABELS[choice] || choice
  if (spec.key === 'ui.catalog.density') return DENSITY_LABELS[choice] || choice
  return choice
}

/**
 * No option is disabled today.
 *
 * `dashboard` was disabled from V1.2 until V1.5 with the note "Ships with the
 * Command Center dashboard", because saving a landing page that did not exist
 * would have left `/` rendering nothing. The dashboard now exists. Kept as a
 * function rather than deleted: the next setting whose consumer lands a chunk
 * later needs exactly this, and re-deriving it is how a live-looking control
 * for something unbuilt gets shipped instead.
 */
function optionDisabled() {
  return false
}

function optionNote(spec, choice) {
  return optionDisabled(spec, choice) ? 'Not available yet' : ''
}

function apply(data) {
  specs.value = data.settings
  environment.value = data.environment
  providers.value = data.providers || []
  sections.value = [...data.sections, 'Access']
  authMode.value = data.auth_mode
  saved.value = Object.fromEntries(data.settings.map((s) => [s.key, s.value]))
  draft.value = { ...saved.value }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    // Routed through the write gate even though this is a READ. Settings is the
    // only admin-scoped read in the app, so an anonymous visitor who clicks
    // Settings would otherwise land on an error telling them they need a token
    // with nothing to type it into — the gate opens on refused *writes*, and
    // this is not one. Parking the load means the prompt appears and the screen
    // fills in by itself once the token is accepted.
    const data = await withWriteGate(() => api.getSettings())
    // Falsy means the prompt was dismissed rather than failed.
    if (!data) {
      error.value = 'The settings screen needs the admin token.'
      return
    }
    apply(data)
  } catch (err) {
    error.value = err.message || 'Could not load settings.'
  } finally {
    loading.value = false
  }
}

onMounted(load)

function set(key, value) {
  draft.value = { ...draft.value, [key]: value }
}

function step(spec, delta) {
  const next = (draft.value[spec.key] ?? spec.default) + delta * (spec.step || 1)
  // Mirrors the server floor so the stepper cannot offer an invalid value —
  // a convenience, not the boundary. The API refuses it either way (§6a).
  set(spec.key, Math.max(spec.minimum ?? 1, next))
}

/**
 * Typed input for an int setting.
 *
 * **Accepted while typing, clamped only on blur.** Clamping per keystroke means
 * that typing `300` into a field with a minimum of 30 rewrites the first `3` to
 * `30` and you spend the rest of the entry fighting the control. The floor is
 * still enforced — just at the point the value is finished, and by the API
 * regardless (§6a: the stepper is a convenience, the API is the boundary).
 */
function onIntInput(spec, event) {
  const raw = event.target.value
  if (raw === '') return // let it be empty mid-edit; blur resolves it
  const value = Number(raw)
  if (Number.isInteger(value)) set(spec.key, value)
}

/** Digits only, as-you-type. The server refuses anything else regardless —
 *  the input is a convenience, the API is the boundary (§6a). */
function onMentionInput(spec, event) {
  const digits = String(event.target.value).replace(/\D/g, '').slice(0, 20)
  set(spec.key, digits)
  event.target.value = digits
}

function onIntBlur(spec, event) {
  const floor = spec.minimum ?? 1
  // `Number('')` is 0, not NaN — so an empty field would pass the integer check
  // below and clamp to the floor instead of being restored. Check the string.
  const raw = String(event.target.value).trim()
  const value = raw === '' ? NaN : Number(raw)
  let next
  if (!Number.isInteger(value)) {
    // Empty or nonsense — put back what was there rather than inventing a
    // number. Restoring the saved value reads as undo; jumping to the floor
    // reads as the app choosing for you.
    next = saved.value[spec.key] ?? spec.default
  } else {
    next = Math.max(floor, value)
  }
  set(spec.key, next)
  // The bound value may not have changed (typing "5" into a field showing 30
  // leaves draft at 30), so Vue would not re-render the input. Write it back.
  event.target.value = next
}

function discard() {
  draft.value = { ...saved.value }
}

async function save() {
  const payload = Object.fromEntries(changed.value.map((s) => [s.key, draft.value[s.key]]))
  saving.value = true
  error.value = ''
  try {
    const data = await withWriteGate(() => api.updateSettings(payload))
    if (!data) return
    apply(data)
    state.authMode = data.auth_mode
    // The rest of the app reads display preferences from the PUBLIC settings
    // in the store — refresh them, or a changed density/landing page only
    // shows up after a manual reload (validation 2026-08-07, finding 3).
    await loadPublicSettings()
  } catch (err) {
    error.value = err.message || 'Could not save.'
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <main class="content settings-screen">
    <div class="page-head">
      <div>
        <div class="page-title">Settings</div>
        <div class="counts">
          Admin token required. Saved changes affect every operator and every
          dashboard, except where a row is marked device-local.
        </div>
      </div>
      <button class="btn-ghost" style="margin-left: auto" @click="emit('back')">
        ← Back to the catalog
      </button>
    </div>

    <div v-if="error" class="notice err">{{ error }}</div>

    <!-- §6c: the mode cannot be fixed from this screen, so the warning names
         the variable to edit and stays up. -->
    <div v-if="authMode !== 'full'" class="notice warn settings-banner">
      <strong>Auth mode is {{ authMode }} — writes are unauthenticated.</strong>
      Anyone on this network can add or edit services, and every added service
      makes this server fetch a URL of their choosing. Set
      <code>HOMELABVAULT_AUTH_MODE=full</code> in <code>.env</code> and restart to
      require a token.
    </div>

    <div v-if="loading" class="spinner" style="margin-top: 24px">Loading settings…</div>

    <!-- The cards scroll; the save bar below is a sibling, not the last child of
         the scrolling box. `position: sticky; bottom:` on a final child does
         nothing — sticky can only float an element within whatever is left of
         its parent's box, and after the last element there is nothing left. -->
    <div v-else class="settings-scroll">
      <!-- TN-SET: the section rail. Desktop: a sticky left sub-rail; phone: the
           same element as a horizontal chip strip (the adapter-bar behaviour).
           A warn dot marks a section holding unsaved edits, because the savebar
           is global and the edits may live off-screen. -->
      <nav class="settings-rail" aria-label="Settings sections">
        <button
          v-for="item in railItems"
          :key="item.slug"
          class="settings-rail-item"
          :class="{ active: item.label === activeSection }"
          @click="goSection(item)"
        >
          <span class="settings-rail-label">{{ item.label }}</span>
          <span
            v-if="dirtySections.has(item.label)"
            class="settings-rail-dirty"
            title="Unsaved changes in this section"
          />
        </button>
      </nav>

      <div class="settings-body">
      <section v-for="section in visibleSections" :key="section" class="card settings-card">
        <header class="settings-card-head">
          <div class="settings-card-title">{{ section }}</div>
          <span
            class="settings-badge"
            :class="badgeFor(section).tone"
            :title="badgeFor(section).hint"
          >
            {{ badgeFor(section).label }}
          </span>
          <span class="settings-aside">{{ asideFor(section) }}</span>
        </header>

        <!-- Theme is device-local: localStorage, never the server (AS_BUILT §3
             row 6). The mockup badges it THIS DEVICE for exactly this reason. -->
        <div v-if="section === 'Appearance'" class="settings-row">
          <div class="settings-row-main">
            <div class="settings-row-label">
              Theme
              <span class="settings-tag">THIS DEVICE</span>
            </div>
            <div class="settings-row-helper">
              Remembered in this browser only. Applies the moment you pick it and
              is never saved to the server.
            </div>
          </div>
          <div class="seg">
            <button :class="{ on: theme === 'light' }" @click="theme = 'light'">Light</button>
            <button :class="{ on: theme === 'dark' }" @click="theme = 'dark'">Dark</button>
          </div>
        </div>

        <div
          v-for="spec in specsBySection[section] || []"
          :key="spec.key"
          class="settings-row"
          :class="{ inert: !!inertReason(spec) }"
        >
          <div class="settings-row-main">
            <div class="settings-row-label">
              {{ spec.label }}
              <span v-if="draft[spec.key] !== saved[spec.key]" class="settings-changed">
                changed
              </span>
            </div>
            <div class="settings-row-helper">{{ spec.helper }}</div>
            <div v-if="inertReason(spec)" class="settings-note">{{ inertReason(spec) }}</div>
          </div>

          <button
            v-if="spec.kind === 'bool'"
            class="settings-toggle"
            :class="{ on: draft[spec.key] && !inertReason(spec) }"
            :disabled="!!inertReason(spec)"
            :aria-pressed="String(!!draft[spec.key])"
            :aria-label="spec.label"
            @click="set(spec.key, !draft[spec.key])"
          >
            <span class="settings-knob" />
          </button>

          <div v-else-if="spec.kind === 'enum'" class="seg">
            <button
              v-for="choice in optionsFor(spec)"
              :key="choice"
              :class="{ on: draft[spec.key] === choice }"
              :disabled="optionDisabled(spec, choice)"
              :title="optionNote(spec, choice)"
              @click="set(spec.key, choice)"
            >
              {{ optionLabel(spec, choice) }}
            </button>
          </div>

          <!-- Typable, not stepper-only. Going from 120 to 900 on a stepper
               with step=30 is 26 clicks, which is not a control, it is a
               punishment. The arrows stay for nudging. -->
          <div v-else-if="spec.kind === 'int'" class="settings-stepper">
            <input
              class="settings-stepper-input mono"
              type="number"
              inputmode="numeric"
              :value="draft[spec.key]"
              :min="spec.minimum ?? 1"
              :step="spec.step || 1"
              :disabled="!!inertReason(spec)"
              :aria-label="spec.label"
              @input="onIntInput(spec, $event)"
              @blur="onIntBlur(spec, $event)"
            />
            <span class="settings-stepper-unit">{{ spec.unit }}</span>
            <span class="settings-stepper-buttons">
              <button :disabled="!!inertReason(spec)" aria-label="Increase" @click="step(spec, 1)">▲</button>
              <button :disabled="!!inertReason(spec)" aria-label="Decrease" @click="step(spec, -1)">▼</button>
            </span>
          </div>

          <!-- A Discord snowflake ID or empty. Digits only as a convenience;
               the API refuses anything else either way (the kind is a shape
               check, like env_ref — never free text). -->
          <input
            v-else-if="spec.kind === 'discord_mention'"
            class="settings-mention mono"
            type="text"
            inputmode="numeric"
            maxlength="20"
            placeholder="Empty — nobody"
            :value="draft[spec.key]"
            :aria-label="spec.label"
            @input="onMentionInput(spec, $event)"
          />

          <div v-else class="settings-envref mono">{{ draft[spec.key] }}</div>
        </div>

        <!-- Derived, not stored (§5a). Reads the DRAFT, so stepping the interval
             moves this line before anything is saved — the number an operator is
             about to commit is the number they see. -->
        <div v-if="section === 'Health checks'" class="settings-row">
          <div class="settings-row-main">
            <div class="settings-row-label">
              Worker stall alert
              <span class="settings-tag">DERIVED</span>
            </div>
            <div class="settings-row-helper">
              If the health worker stops reporting for
              <strong>{{ humanSeconds(stallAfterS) }}</strong> — three poll
              intervals — Homelab Vault says so. Nothing to configure: raise the
              interval and this moves with it.
            </div>
          </div>
          <div class="settings-derived-value mono">{{ stallAfterS }}s</div>
        </div>

        <!-- The mockup's "Sends a message when" block. Every line is derived
             from the values above rather than written out, so the copy cannot
             drift from the behaviour (§5a applies to the wording too). -->
        <div v-if="section === 'Notifications'" class="settings-triggers">
          <div class="settings-triggers-title">Sends a message when</div>
          <ul>
            <li v-for="trigger in notificationTriggers" :key="trigger">{{ trigger }}</li>
          </ul>
          <div class="settings-note">
            Services with notifications switched off are still checked and still
            show their real status — they just never post. The channel is
            whichever one the webhook URL belongs to.
          </div>
        </div>

        <!-- Provider health, from the worker's cached snapshot (§7c) — this
             screen never calls Proxmox. "Configured" alone is not enough: an
             under-privileged token authenticates and sees nothing, and Proxmox
             reports that as an empty list rather than an error. -->
        <div v-if="section === 'Proxmox' && proxmoxStatus" class="settings-triggers">
          <div class="settings-triggers-title">Provider status</div>
          <div class="settings-provider-row">
            <span
              class="settings-badge"
              :class="proxmoxStatus.usable ? 'accent' : 'warn'"
            >
              {{ proxmoxStatus.usable ? 'Working' : 'Not working' }}
            </span>
            <span v-if="proxmoxStatus.usable" class="settings-provider-facts mono">
              {{ proxmoxStatus.guest_count }} guests
              <template v-if="proxmoxStatus.ambiguous_address_count">
                · {{ proxmoxStatus.ambiguous_address_count }} shared addresses skipped
              </template>
            </span>
          </div>
          <!-- What the credential can actually do, from the last successful
               poll (Window 2 serialised it; this renders it). It sits beside
               the Working/Not working badge because a blind token is a
               privileges problem, and this is the screen where that gets
               diagnosed — the pveum fix in `detail` names the role, this
               names what the token currently holds. -->
          <div
            v-if="proxmoxStatus.privileges?.length"
            class="settings-provider-facts mono"
          >
            Token privileges: {{ proxmoxStatus.privileges.join(' · ') }}
          </div>
          <div v-if="proxmoxStatus.detail" class="settings-note settings-provider-detail">
            {{ proxmoxStatus.detail }}
          </div>
          <!-- Audit S5: the cluster read can fail while the guest list works
               (`usable` stays true). The dashboard says "see Settings for the
               reason" — this is that reason, and before it was rendered here
               the pointer led to a screen that showed nothing. -->
          <div
            v-if="proxmoxStatus.infrastructure_detail"
            class="settings-note settings-provider-detail"
          >
            <strong>Cluster panel:</strong> {{ proxmoxStatus.infrastructure_detail }}
            Guest locations keep working — only the dashboard's cluster and node
            panels are affected.
          </div>
          <!-- Stated, not discovered (§7b). Neither limit is an edge case: on a
               typical cluster a couple of Docker hosts carry a large share of
               the catalog, so "the host, not this service" is what most derived
               locations mean. -->
          <div class="settings-note">
            A service gets a location when its address matches exactly one guest.
            Containers running Docker resolve to their <strong>host</strong>, not
            to themselves, so services sharing a host share its location. VMs need
            the QEMU guest agent running to report an address. An address held by
            more than one guest identifies none of them and is skipped. A typed
            Location on a service pins it and stops it tracking.
          </div>
        </div>

        <!-- §6c's three modes, stated where an operator chooses one.
             Grouped as ONE exclusive choice (BACKLOG.4, VALIDATION_V1.3
             finding 8): a bordered radio-group frame with one filled dot,
             instead of three rows that blurred into the .env rows below.
             The dots are indicators, not inputs — the mode is set in .env
             and cannot be changed from here at any privilege level. -->
        <div v-if="section === 'Access'" class="settings-modes" role="radiogroup" aria-label="Auth mode">
          <div class="settings-modes-caption">
            One mode is active at a time — set with
            <code>HOMELABVAULT_AUTH_MODE</code> in <code>.env</code>, applied at
            restart.
          </div>
          <div
            v-for="mode in AUTH_MODES"
            :key="mode.key"
            class="settings-mode"
            role="radio"
            :aria-checked="String(mode.key === authMode)"
            :class="{
              active: mode.key === authMode,
              risky: mode.key === authMode && mode.key !== 'full',
            }"
          >
            <span class="settings-mode-radio" aria-hidden="true" />
            <span class="settings-mode-name mono">{{ mode.key }}</span>
            <span class="settings-mode-desc">{{ mode.desc }}</span>
            <span v-if="mode.key === authMode" class="settings-mode-active">ACTIVE</span>
          </div>
          <!-- The real cost of an open mode, stated plainly (§6c). Operator
               informed consent, not a footnote. -->
          <div class="settings-warn" :class="{ live: authMode !== 'full' }">
            In <code>open-writes</code> and <code>open</code> modes, anyone who can
            reach this server can register a service and make the server fetch an
            arbitrary internal URL on their behalf. Only run them on a network you
            fully trust, and keep <code>PROBE_ALLOWED_NETWORKS</code> as tight as
            possible.
          </div>
        </div>

        <!-- Recessed: owned by .env, shown here so §6a is visible. -->
        <div
          v-for="row in envBySection[section] || []"
          :key="row.env_var"
          class="settings-env"
        >
          <div class="settings-env-head">
            <div>
              <div class="settings-env-label">{{ row.label }}</div>
              <div class="settings-env-var mono">{{ row.env_var }}</div>
            </div>
            <div class="settings-env-status">
              <span
                class="settings-status-pill"
                :class="{
                  ok: row.status === 'Configured',
                  warn: row.status === 'Not configured',
                }"
              >
                {{ row.status }}
              </span>
              <span class="settings-tag">MANAGED IN .ENV</span>
            </div>
          </div>
          <div class="settings-env-note">{{ row.note }}</div>
        </div>
      </section>

      <!-- Moved off the top bar (V1.5 validation, finding 5). Both are
           occasional, deliberate actions rather than navigation, so they belong
           on the screen you visit on purpose. Frontend-owned, so it is gated on
           the active section here rather than riding the backend section list. -->
      <section v-if="activeSection === LINKS_SECTION" class="card settings-card">
        <div class="settings-card-head">
          <div class="settings-card-title">Data and API</div>
          <span class="settings-badge muted" title="These open in a new tab; nothing here changes any setting.">
            Links
          </span>
        </div>
        <div class="settings-row">
          <div class="settings-row-main">
            <div class="settings-row-label">Export the catalog as JSON</div>
            <div class="settings-row-helper">
              Every service, unauthenticated and unfiltered — the same data the
              catalog screen loads. Settings are deliberately excluded.
            </div>
          </div>
          <a class="btn-ghost" href="/api/v1/export/json" target="_blank" rel="noopener">
            Export JSON ↗
          </a>
        </div>
        <div class="settings-row">
          <div class="settings-row-main">
            <div class="settings-row-label">Browse the API</div>
            <div class="settings-row-helper">
              The generated OpenAPI schema, which is this project's canonical API
              reference — there is no hand-written endpoint list to drift from it.
            </div>
          </div>
          <a class="btn-ghost" href="/api/v1/docs" target="_blank" rel="noopener">
            API docs ↗
          </a>
        </div>
      </section>
      </div>
    </div>

    <div v-if="dirty" class="settings-savebar">
      <span class="settings-dot" />
      <div>
        <div class="settings-savebar-title">
          {{ changed.length === 1 ? '1 unsaved change' : `${changed.length} unsaved changes` }}
        </div>
        <div class="settings-savebar-list">
          {{ changed.map((s) => s.label).join(' · ') }}
        </div>
      </div>
      <button class="btn-ghost" style="margin-left: auto" @click="discard">Discard</button>
      <button class="btn-primary" :disabled="saving" @click="save">
        {{ saving ? 'Saving…' : 'Save changes' }}
      </button>
    </div>
  </main>
</template>
