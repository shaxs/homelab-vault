<script setup>
/**
 * Catalog table — spec §5, Layout 2B.
 *
 * Columns: checkbox · Service · Category · Status · Location · Tags · Launch.
 * Category became its own column on 2026-08-07 — which is what v0 spec §5 asked
 * for originally; it had been a subline under the service name because the
 * mockup drew it that way, and the pair read as one crowded block. The checkbox drives §6b's row multi-select
 * "add tag", and clicking a tag appends `tag:x` to the active query.
 *
 * Service-supplied text (name, category, host_label, tags) is rendered with
 * {{ }} only. It can originate from a probed page title, i.e. from a third
 * party — never render it with v-html.
 *
 * Clicking the icon or name LAUNCHES the service (2026-10-28, operator
 * request): it is where the eye and the cursor land, and it used to open the
 * record, so the common action took a detour. The record moved to the **Edit**
 * button beside Open, which opens it straight into edit mode. A service whose
 * URL cannot be launched opens in edit mode instead of doing nothing — the
 * URL is the thing to fix. The Location cell still opens the record to view.
 */
import { computed, ref } from 'vue'

import { safeUrl } from '../format.js'
import { launch, recheckHealth, state } from '../store.js'
import StatusPill from './StatusPill.vue'

/** `ui.catalog.density` (§6a public whitelist): server-wide, read from the
 *  anonymous settings endpoint like the landing page. Compact is the original
 *  Layout 2B table; comfortable scales rows, icons and text up. */
const comfortable = computed(
  () => state.publicSettings['ui.catalog.density'] === 'comfortable',
)

/** Slugs currently being re-checked, so each row can show its own spinner. */
const checking = ref(new Set())

async function recheck(service) {
  if (!service.health_enabled || checking.value.has(service.slug)) return
  checking.value = new Set(checking.value).add(service.slug)
  try {
    await recheckHealth(service.slug)
  } catch (err) {
    // try…finally with no catch left a failed recheck as an unhandled
    // rejection and a spinner that just stopped (audit S11). Same surface as
    // the bulk recheck in App.vue.
    state.error = err.message || 'Could not re-check.'
  } finally {
    const next = new Set(checking.value)
    next.delete(service.slug)
    checking.value = next
  }
}

const props = defineProps({
  rows: { type: Array, required: true },
  total: { type: Number, required: true },
})
const emit = defineEmits(['open', 'edit', 'append-term', 'select-term'])

/** `launch()` silently ignores a URL `safeUrl` rejects; here that would be a
 *  click that does nothing, so send the operator to fix it instead. */
function launchOrEdit(service, event) {
  if (safeUrl(service.launch_url)) launch(service, event.metaKey || event.ctrlKey)
  else emit('edit', service)
}

function toggle(slug) {
  if (state.selected.has(slug)) state.selected.delete(slug)
  else state.selected.add(slug)
  // Reassign so the reactive Set change is observed.
  state.selected = new Set(state.selected)
}

function initials(name) {
  return (name || '?').replace(/[^A-Za-z0-9 ]/g, '').slice(0, 2).toUpperCase()
}
</script>

<template>
  <div class="card table" :class="{ 'density-comfortable': comfortable }">
    <div class="row head">
      <div />
      <div class="col-head">Service</div>
      <div class="col-head">Category</div>
      <div class="col-head">Status</div>
      <div class="col-head">Location</div>
      <div class="col-head">Tags</div>
      <div class="col-head right">Launch</div>
    </div>

    <div v-for="service in rows" :key="service.slug" class="row body">
      <button
        class="checkbox"
        :class="{ on: state.selected.has(service.slug) }"
        :aria-label="`Select ${service.name}`"
        @click.stop="toggle(service.slug)"
      >
        {{ state.selected.has(service.slug) ? '✓' : '' }}
      </button>

      <div
        class="svc"
        :title="safeUrl(service.launch_url) ? `Open ${service.name}` : 'No launchable URL — edit it'"
        @click="launchOrEdit(service, $event)"
      >
        <span class="svc-icon">
          <img v-if="service.icon_url" :src="service.icon_url" alt="" />
          <template v-else>{{ initials(service.name) }}</template>
        </span>
        <span style="min-width: 0">
          <div class="svc-name">{{ service.name }}</div>
        </span>
      </div>

      <!-- Its own column as of 2026-08-07, which is what the v0 spec's column
           list said all along (§5: "Service, Status, Location, Category,
           Launch"). It was a subline under the service name because the mockup
           drew it that way, and the two lines read as one crowded block.
           Clickable like the tag chips, and single-select like the sidebar. -->
      <div class="cat-cell">
        <button
          v-if="service.category"
          class="cat-chip"
          :title="`Show only ${service.category}`"
          @click.stop="emit('select-term', `cat:${service.category}`)"
        >
          {{ service.category }}
        </button>
        <span v-else class="cat-empty">—</span>
      </div>

      <!-- Clicking the status re-checks it rather than opening the record: it
           is the one place in the row where you are already thinking about
           health, and waiting out the interval to confirm a fix is tedious. -->
      <div
        class="status-cell"
        :class="{ busy: checking.has(service.slug) }"
        :title="service.health_enabled ? 'Check now' : 'Health checks are disabled'"
        @click.stop="recheck(service)"
      >
        <StatusPill :service="service" />
        <span v-if="checking.has(service.slug)" class="status-spin">↻</span>
      </div>

      <!-- Pinned wins on display (§4a); derived fills in when nothing is pinned.
           Without the fallback this column reads "—" for every service on a
           catalog where nothing is pinned — which is the normal state once
           Proxmox derives location, since unpinned is the default. A derived
           value the provider has stopped reporting says so (audit S8): the
           location is still served, but "stale-and-said beats silently
           current-looking". The tag never applies to a pin — that value is the
           operator's, and the provider has no opinion about it. -->
      <div class="host" @click="emit('open', service)">
        {{ service.host_label || service.location_derived || '—' }}
        <span
          v-if="!service.host_label && service.location_derived && service.location_stale"
          class="loc-tag warn"
          title="Proxmox has not reported this guest for several polls. The location may be out of date; if the guest stays missing the link is dropped."
        >stale</span>
      </div>

      <div class="tags">
        <button
          v-for="tag in service.tags"
          :key="tag"
          class="tag"
          @click.stop="emit('append-term', `tag:${tag}`)"
        >
          {{ tag }}
        </button>
      </div>

      <div class="launch">
        <button class="edit-btn" @click.stop="emit('edit', service)">Edit</button>
        <button
          class="launch-btn"
          @click.stop="launch(service, $event.metaKey || $event.ctrlKey)"
        >
          Open ↗
        </button>
      </div>
    </div>

    <div class="table-foot">
      <span>{{ rows.length }} of {{ total }} · filtered in the browser</span>
      <span style="margin-left: auto">Whole catalog loaded once</span>
    </div>
  </div>
</template>
