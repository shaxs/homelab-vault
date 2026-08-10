/**
 * Effective health status — the client-side mirror of spec §8.
 *
 * The API already derives `stale` on read, but it does so at serialize time.
 * A catalog left open in a tab holds a snapshot, so without re-deriving here a
 * page loaded an hour ago would keep reporting "Up" long after the worker died
 * — which is exactly the failure §8 introduced `stale` to surface.
 *
 * The rule is deliberately identical to Service.effective_status():
 *
 *     checks disabled                                  -> off
 *     never checked                                    -> unknown
 *     checked_at older than 3 x the effective interval -> stale
 *     otherwise                                        -> the stored status
 *
 * It derives from the raw stored fields, never from a previously derived
 * value, so applying it repeatedly to the same record is idempotent.
 *
 * **The interval is `health_interval_effective_s`, not `health_interval_s`.**
 * Since v1.3 a service's own interval is nullable and null means "follow the
 * global setting", so the raw column is null for most services — falling back to
 * a hardcoded 60 here would make the browser disagree with the server for every
 * deployment that changed the interval, and disagree *silently*, which is the
 * one thing a status is not allowed to do.
 */

export const STATUS_ORDER = ['down', 'stale', 'unknown', 'up', 'off']

export function effectiveStatus(service, at = Date.now()) {
  if (!service) return 'unknown'
  if (service.health_enabled === false) return 'off'
  if (!service.health_checked_at) return 'unknown'

  const checkedAt = new Date(service.health_checked_at).getTime()
  if (Number.isNaN(checkedAt)) return service.health_status || 'unknown'

  const interval = Math.max(
    Number(service.health_interval_effective_s) || Number(service.health_interval_s) || 60,
    1,
  )
  if (at - checkedAt > interval * 3 * 1000) return 'stale'

  return service.health_status || 'unknown'
}

/** Re-derive `status` across a catalog. Returns the same array identity when
 *  nothing changed, so Vue does not re-render on every tick. */
export function applyDerivedStatus(services, at = Date.now()) {
  let changed = false
  const next = services.map((service) => {
    const status = effectiveStatus(service, at)
    if (status === service.status) return service
    changed = true
    return { ...service, status }
  })
  return changed ? next : services
}
