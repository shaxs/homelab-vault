/**
 * API client — spec §4c.
 *
 * The UI is a client of the public API and gets no privileged path to the
 * database. Writes go out with the CSRF token, which Django issues as a cookie
 * on the SPA page load (see services/views.py @ensure_csrf_cookie).
 */

function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/)
  return match ? decodeURIComponent(match[1]) : ''
}

export class ApiError extends Error {
  constructor(status, body) {
    super(ApiError.describe(status, body))
    this.status = status
    this.body = body
  }

  /** Turn Ninja's 422 detail array into something a person can act on. */
  static describe(status, body) {
    if (status === 401 || status === 403) return 'Not authorised.'
    if (body && Array.isArray(body.detail)) {
      return body.detail
        .map((d) => {
          const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : ''
          return field ? `${field}: ${d.msg}` : d.msg
        })
        .join('; ')
    }
    if (body && typeof body.detail === 'string') return body.detail
    return `Request failed (${status}).`
  }

  get needsAuth() {
    return this.status === 401 || this.status === 403
  }

  /**
   * The admin token must be re-presented before this specific action (§6b),
   * as opposed to "you are not signed in at all".
   *
   * Both open the same modal around the same token field, and without the code
   * the modal cannot tell the two apart — so it would tell an operator who is
   * demonstrably signed in that they are not.
   */
  get needsReauth() {
    return this.status === 403 && this.body?.code === 'reauth_required'
  }
}

async function request(method, path, payload, formData) {
  const options = {
    method,
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  }
  if (formData) {
    // Deliberately no Content-Type: fetch sets multipart/form-data with the
    // boundary, and overriding it breaks the upload.
    options.body = formData
  } else if (payload !== undefined) {
    options.headers['Content-Type'] = 'application/json'
    options.body = JSON.stringify(payload)
  }
  if (method !== 'GET') {
    options.headers['X-CSRFToken'] = csrfToken()
  }

  // The one place the version prefix is written (spec §3). Every call in this
  // module routes through here, so the SPA is a "real consumer" calling
  // /api/v1/ directly rather than riding the compatibility redirect.
  const response = await fetch(`/api/v1${path}`, options)
  let body = null
  const text = await response.text()
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = { detail: text.slice(0, 200) }
    }
  }
  if (!response.ok) throw new ApiError(response.status, body)
  return body
}

export const api = {
  listServices: () => request('GET', '/services'),
  getService: (slug) => request('GET', `/services/${encodeURIComponent(slug)}`),
  upsertService: (payload) => request('POST', '/services', payload),
  patchService: (slug, payload) =>
    request('PATCH', `/services/${encodeURIComponent(slug)}`, payload),
  deleteService: (slug) =>
    request('DELETE', `/services/${encodeURIComponent(slug)}`),

  renameCategory: (name, next) =>
    request('PATCH', `/categories/${encodeURIComponent(name)}`, { name: next }),
  clearCategory: (name) =>
    request('DELETE', `/categories/${encodeURIComponent(name)}`),
  renameTag: (name, next) =>
    request('PATCH', `/tags/${encodeURIComponent(name)}`, { name: next }),
  deleteTag: (name) => request('DELETE', `/tags/${encodeURIComponent(name)}`),

  listViews: () => request('GET', '/views'),
  createView: (payload) => request('POST', '/views', payload),
  renameView: (name, next) =>
    request('PATCH', `/views/${encodeURIComponent(name)}`, { name: next }),
  deleteView: (name) => request('DELETE', `/views/${encodeURIComponent(name)}`),

  recheck: (slug) =>
    request('POST', `/health/${encodeURIComponent(slug)}/check`),

  probe: (url) => request('POST', '/probe', { url }),
  fetchIcon: (url) => request('POST', '/icon', { url }),
  refetchServiceIcon: (slug) =>
    request('POST', `/services/${encodeURIComponent(slug)}/refetch-icon`),
  uploadIcon: (file) => {
    const form = new FormData()
    form.append('file', file)
    // No Content-Type header: the browser must set the multipart boundary.
    return request('POST', '/icon/upload', undefined, form)
  },
  slugPreview: (launchUrl) =>
    request('GET', `/slug-preview?launch_url=${encodeURIComponent(launchUrl)}`),

  startSession: (token) => request('POST', '/session', { token }),
  endSession: () => request('DELETE', '/session'),

  // Admin-scoped (§6a). The catalog never calls these.
  getSettings: () => request('GET', '/settings'),
  updateSettings: (values) => request('PATCH', '/settings', values),
  // Anonymous, and deliberately narrow: display keys plus the auth mode.
  getPublicSettings: () => request('GET', '/settings/public'),

  // §8: the dashboard's ONE read. Panels are assembled and cached server-side;
  // the browser never calls a provider, which is the v1 decision v2 depends on.
  getPanels: () => request('GET', '/panels'),

  // Per-worker liveness for the sidebar. Public, and deliberately so (§5a) —
  // the anonymous body carries name/state/last beat and withholds the
  // thresholds. Answers 503 while any worker is stalled, which `request`
  // would throw on, so callers must handle that as data rather than failure.
  getHealthz: () => request('GET', '/healthz'),
}
