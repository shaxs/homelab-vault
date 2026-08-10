<script setup>
/**
 * Detail drawer — v1 §7d.
 *
 * **Its actions are a list, not three hardcoded rows** (§7d, §12). That is the
 * whole reason this component takes `actions` as data: §7d defers Logs and Docs
 * because there is no data model behind either, and says in as many words that
 * a later OpenObserve or Loki adapter should land by appending to a list. One
 * action ships today ("Open external"), and a node has none — which is what
 * proves the list is really a list.
 *
 * **A drawer, not a modal.** `BaseModal` owns the backdrop and traps the page;
 * this sits beside the content so a node's numbers stay readable next to the
 * table they came from. DESIGN_SYSTEM's "never build a one-off modal" rule is
 * about modals, and this is not one — it takes no backdrop and blocks nothing.
 *
 * Every value here can originate from a probed third-party page title, so it is
 * rendered with {{ }} only. Never v-html.
 */
defineProps({
  title: { type: String, required: true },
  subtitle: { type: String, default: '' },
  /** Optional { label, tone } — tone is one of ok | warn | err | muted. */
  status: { type: Object, default: null },
  /** [{ key, value, mono, href }] — `href` renders the value as a link. */
  rows: { type: Array, default: () => [] },
  /** [{ label, href, mark }] — empty is a valid state, not a bug. */
  actions: { type: Array, default: () => [] },
})

defineEmits(['close'])
</script>

<template>
  <aside class="drawer" aria-label="Details">
    <div class="drawer-head">
      <div class="drawer-heading">{{ title }}</div>
      <button class="drawer-close" aria-label="Close details" @click="$emit('close')">
        ✕
      </button>
    </div>

    <div class="card drawer-card">
      <div class="drawer-title-row">
        <div>
          <div class="drawer-name">{{ title }}</div>
          <div v-if="subtitle" class="drawer-sub">{{ subtitle }}</div>
        </div>
      </div>

      <div v-if="status" class="drawer-row">
        <span class="drawer-key">Status</span>
        <span class="pill" :class="`st-${status.tone}`" style="margin-left: auto">
          <span class="pill-dot" />
          <span class="pill-label">{{ status.label }}</span>
        </span>
      </div>

      <div v-for="row in rows" :key="row.key" class="drawer-row">
        <span class="drawer-key">{{ row.key }}</span>
        <a
          v-if="row.href"
          class="drawer-value link"
          :href="row.href"
          target="_blank"
          rel="noopener"
        >
          {{ row.value }} ↗
        </a>
        <span v-else class="drawer-value">{{ row.value }}</span>
      </div>

      <div class="drawer-actions">
        <div class="col-head">Actions</div>
        <a
          v-for="action in actions"
          :key="action.label"
          class="drawer-action"
          :href="action.href"
          target="_blank"
          rel="noopener"
        >
          <span class="drawer-action-mark" />
          <span style="flex: 1">{{ action.label }}</span>
          <span class="drawer-action-arrow">{{ action.mark || '↗' }}</span>
        </a>
        <div v-if="!actions.length" class="drawer-empty">
          No actions for this item yet.
        </div>
      </div>
    </div>
  </aside>
</template>
