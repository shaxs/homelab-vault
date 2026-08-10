<script setup>
/**
 * Empty state — spec §4b, built in week 1 rather than retrofitted.
 *
 * This is the §11 acceptance test in visual form: a stranger should be able to
 * `docker compose up`, land here, and add their first service without reading
 * source or touching the database.
 */
defineEmits(['add'])
</script>

<template>
  <div class="empty-wrap">
    <div class="empty">
      <div class="empty-mark">H</div>
      <h1>Nothing catalogued yet</h1>
      <p>
        Homelab Vault is a launcher: find a service, see if it's alive, open it.
        Add the first one and the catalog, search, and health checks all start
        working — there is nothing else to configure.
      </p>
      <button class="btn-primary" @click="$emit('add')">＋ Add your first service</button>

      <div class="empty-steps">
        <div class="empty-step">
          <div class="empty-step-n">01</div>
          <div class="empty-step-t">Paste a URL</div>
          <div class="empty-step-d">
            The URL is the only required field. The name, slug, icon, and a
            category guess are derived for you.
          </div>
        </div>
        <div class="empty-step">
          <div class="empty-step-n">02</div>
          <div class="empty-step-t">Search with ⌘K</div>
          <div class="empty-step-d">
            Bare words fuzzy-match. <code>host:</code> <code>cat:</code>
            <code>tag:</code> <code>state:</code> narrow it. Save any query as a
            view.
          </div>
        </div>
        <div class="empty-step">
          <div class="empty-step-n">03</div>
          <div class="empty-step-t">Let the worker check</div>
          <div class="empty-step-d">
            Run <code>manage.py health_worker</code> and dead services become
            visibly dead before you click them.
          </div>
        </div>
      </div>

      <p style="margin-top: 26px; font-size: 11.5px; color: var(--txt3)">
        Have a list already? Use <strong>Bulk import</strong> to paste one URL
        per line and tag the whole batch at once.
      </p>
    </div>
  </div>
</template>
