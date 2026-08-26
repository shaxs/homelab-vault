/**
 * The settings screen's derived copy — v1 §5a.
 *
 * §5a's rule that a derived value must be re-derived live rather than
 * snapshotted applies "to the notification-trigger copy on the settings screen"
 * as much as to the deadman itself. That makes these assertions the client-side
 * half of the same guarantee the backend proves for the evaluator: text that
 * states a threshold has to move when the threshold moves, or the screen tells
 * an operator something that stopped being true the moment they changed a
 * number.
 *
 * The interesting case is that it re-derives from the **draft**, before saving:
 * an operator stepping the interval is deciding, and the consequence of the
 * value they are looking at is what they need to see.
 */

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api.js'
import { navigate } from '../router.js'
import SettingsScreen from './SettingsScreen.vue'

// The component navigates via the router on rail clicks; mocking it keeps the
// URL out of jsdom and lets the rail tests assert the call (same pattern as
// Dashboard.test.js).
vi.mock('../router.js', () => ({ navigate: vi.fn() }))

const SPECS = [
  {
    key: 'health.default_interval_s',
    kind: 'int',
    value: 60,
    default: 60,
    minimum: 15,
    step: 5,
    unit: 'seconds',
    label: 'Poll interval',
    helper: 'How often the worker probes each health_url.',
    section: 'Health checks',
  },
  {
    key: 'health.fail_threshold',
    kind: 'int',
    value: 3,
    default: 3,
    minimum: 1,
    unit: 'failures',
    label: 'Fail threshold',
    helper: 'Consecutive failed probes before a service is marked down.',
    section: 'Health checks',
  },
  {
    key: 'health.recovery_threshold',
    kind: 'int',
    value: 2,
    default: 2,
    minimum: 1,
    unit: 'successes',
    label: 'Recovery threshold',
    helper: 'Consecutive successful probes before a service returns to up.',
    section: 'Health checks',
  },
  {
    key: 'notifications.discord.enabled',
    kind: 'bool',
    value: false,
    default: false,
    label: 'Discord notifications',
    helper: 'Send state changes to the configured Discord channel.',
    section: 'Notifications',
  },
]

function payload(values = {}, extra = {}) {
  return {
    auth_mode: 'full',
    sections: ['Health checks', 'Notifications'],
    environment: [],
    providers: [],
    settings: SPECS.map((spec) =>
      spec.key in values ? { ...spec, value: values[spec.key] } : spec,
    ),
    ...extra,
  }
}

async function open(values, extra, section) {
  // TN-SET: one section renders at a time, so a test mounts the section its
  // assertions live in. Omitted -> the first section ('Health checks' in the
  // default payload), exactly like the bare /settings route.
  vi.spyOn(api, 'getSettings').mockResolvedValue(payload(values, extra))
  const wrapper = mount(SettingsScreen, { props: section ? { section } : {} })
  await new Promise((resolve) => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('the derived worker-stall row', () => {
  it('states three poll intervals, not a hardcoded number', async () => {
    const wrapper = await open()
    expect(wrapper.text()).toContain('Worker stall alert')
    expect(wrapper.text()).toContain('180s')
    expect(wrapper.text()).toContain('3 minutes')
    wrapper.unmount()
  })

  it('moves when the interval moves', async () => {
    const wrapper = await open({ 'health.default_interval_s': 300 })
    expect(wrapper.text()).toContain('900s')
    expect(wrapper.text()).toContain('15 minutes')
    expect(wrapper.text()).not.toContain('180s')
    wrapper.unmount()
  })

  it('re-derives from the draft, before anything is saved', async () => {
    // The stepper is the operator deciding. A row that only updated on save
    // would describe the value they just left behind.
    const wrapper = await open()
    const increase = wrapper
      .findAll('button')
      .filter((button) => button.attributes('aria-label') === 'Increase')

    await increase[0].trigger('click') // +5s -> 65s
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('195s')
    expect(api.getSettings).toHaveBeenCalledTimes(1) // nothing re-fetched
    wrapper.unmount()
  })
})

describe('the "Sends a message when" block', () => {
  it('describes the thresholds actually configured', async () => {
    const wrapper = await open(
      { 'health.fail_threshold': 5, 'health.recovery_threshold': 1 },
      undefined,
      'notifications',
    )
    const text = wrapper.text()

    expect(text).toContain('Sends a message when')
    expect(text).toContain('fails 5 checks in a row')
    expect(text).toContain('passes 1 check in a row')
    wrapper.unmount()
  })

  it('says a silenced service is still checked', async () => {
    // §4's separation, in the one place an operator reads about notifications.
    const wrapper = await open(undefined, undefined, 'notifications')
    expect(wrapper.text()).toContain('still checked')
    wrapper.unmount()
  })

  it('names the worker stall as a trigger', async () => {
    const wrapper = await open(undefined, undefined, 'notifications')
    expect(wrapper.text()).toContain('stops reporting for 3 minutes')
    wrapper.unmount()
  })
})

describe('the Proxmox provider status card (§7a, §7b)', () => {
  const PROXMOX_SECTION = { sections: ['Health checks', 'Notifications', 'Proxmox'] }

  function withProvider(provider) {
    return { ...PROXMOX_SECTION, providers: [provider] }
  }

  it('reports a working provider with its guest count', async () => {
    const wrapper = await open(
      {},
      withProvider({
        name: 'proxmox',
        configured: true,
        usable: true,
        detail: '',
        guest_count: 37,
        ambiguous_address_count: 6,
        last_polled_at: '2026-08-06T12:00:00Z',
      }),
      'proxmox',
    )
    expect(wrapper.text()).toContain('Working')
    expect(wrapper.text()).toContain('37 guests')
    expect(wrapper.text()).toContain('6 shared addresses skipped')
    wrapper.unmount()
  })

  it('reports a configured-but-blind token as NOT working, with the fix', async () => {
    // The finding this card exists for: Proxmox answers 200 [] for collections
    // it filters by privilege, so an under-privileged token is otherwise
    // indistinguishable from an empty cluster. "Configured" alone would render
    // a green tick over a provider that can see nothing.
    const wrapper = await open(
      {},
      withProvider({
        name: 'proxmox',
        configured: true,
        usable: false,
        detail: "The API token authenticates but holds no privileges. pveum acl modify / --tokens 'x' --roles PVEAuditor",
        guest_count: 0,
        ambiguous_address_count: 0,
        last_polled_at: '2026-08-06T12:00:00Z',
      }),
      'proxmox',
    )
    expect(wrapper.text()).toContain('Not working')
    expect(wrapper.text()).toContain('PVEAuditor')
    expect(wrapper.text()).not.toContain('0 guests')
    wrapper.unmount()
  })

  it('states the known limits whether or not the provider is working', async () => {
    // §7b: stated rather than discovered.
    const wrapper = await open(
      {},
      withProvider({
        name: 'proxmox',
        configured: true,
        usable: true,
        detail: '',
        guest_count: 3,
        ambiguous_address_count: 0,
        last_polled_at: null,
      }),
      'proxmox',
    )
    const text = wrapper.text()
    expect(text).toMatch(/Docker resolve to their|resolve to their/i)
    expect(text).toMatch(/QEMU guest agent/i)
    expect(text).toMatch(/more than one guest/i)
    wrapper.unmount()
  })

  it('lists the token privileges from the last successful poll (Window 3)', async () => {
    // `privileges` has been in the admin provider row since Window 2, computed
    // and asserted server-side but rendered nowhere. Beside the Working badge
    // is where it earns its keep: a blind token is a privileges problem.
    const wrapper = await open(
      {},
      withProvider({
        name: 'proxmox',
        configured: true,
        usable: true,
        detail: '',
        guest_count: 6,
        ambiguous_address_count: 0,
        privileges: ['Sys.Audit', 'VM.Audit', 'VM.GuestAgent.Audit'],
        last_polled_at: '2026-08-06T12:00:00Z',
      }),
      'proxmox',
    )
    const text = wrapper.text()
    expect(text).toContain('Token privileges')
    expect(text).toContain('Sys.Audit · VM.Audit · VM.GuestAgent.Audit')
    wrapper.unmount()
  })

  it('shows no privileges line when the poll has reported none', async () => {
    const wrapper = await open(
      {},
      withProvider({
        name: 'proxmox',
        configured: true,
        usable: false,
        detail: 'The API token authenticates but holds no privileges.',
        guest_count: 0,
        ambiguous_address_count: 0,
        privileges: [],
        last_polled_at: '2026-08-06T12:00:00Z',
      }),
      'proxmox',
    )
    expect(wrapper.text()).not.toContain('Token privileges')
    wrapper.unmount()
  })

  it('renders no provider card at all when none is configured', async () => {
    // §7d: no token -> no panel, nothing to explain away.
    const wrapper = await open({}, { ...PROXMOX_SECTION, providers: [] }, 'proxmox')
    expect(wrapper.text()).not.toContain('Provider status')
    wrapper.unmount()
  })
})

describe('int settings are typable, not stepper-only', () => {
  // Operator finding, 2026-08-06: the value was a <span>, so the arrows were the
  // only way in. Going from 120 to 900 with step=30 is 26 clicks.
  function intInput(wrapper, label = 'Poll interval') {
    return wrapper.findAll('input[type="number"]').find(
      (i) => i.attributes('aria-label') === label,
    )
  }

  it('renders a number input for an int setting', async () => {
    const wrapper = await open()
    expect(intInput(wrapper).exists()).toBe(true)
    expect(intInput(wrapper).element.value).toBe('60')
    wrapper.unmount()
  })

  it('accepts a typed value', async () => {
    const wrapper = await open()
    await intInput(wrapper).setValue('300')
    // The derived stall row reads from the draft, so it proves the draft moved.
    expect(wrapper.text()).toContain('900s')
    wrapper.unmount()
  })

  it('does NOT clamp while typing', async () => {
    // The behaviour that makes the field usable. Minimum is 15; typing "300"
    // passes through "3", and clamping per keystroke would rewrite it to 15 and
    // leave you fighting the control for the rest of the entry.
    const wrapper = await open()
    const field = intInput(wrapper)
    await field.setValue('3')
    expect(field.element.value).toBe('3')
    await field.setValue('300')
    expect(field.element.value).toBe('300')
    wrapper.unmount()
  })

  it('clamps to the floor on blur', async () => {
    const wrapper = await open()
    const field = intInput(wrapper)
    await field.setValue('3')
    await field.trigger('blur')
    expect(field.element.value).toBe('15') // minimum
    wrapper.unmount()
  })

  it('restores the saved value when the field is left empty', async () => {
    // Putting back what was there reads as undo; jumping to the floor reads as
    // the app choosing a number for you.
    const wrapper = await open()
    const field = intInput(wrapper)
    await field.setValue('')
    await field.trigger('blur')
    expect(field.element.value).toBe('60')
    wrapper.unmount()
  })

  it('still nudges with the arrows', async () => {
    const wrapper = await open()
    const up = wrapper.findAll('button').find((b) => b.attributes('aria-label') === 'Increase')
    await up.trigger('click')
    expect(intInput(wrapper).element.value).toBe('65') // step 5
    wrapper.unmount()
  })
})

describe('validation follow-ups (2026-08-07)', () => {
  it('explains what the Mixed badge means on hover', async () => {
    // Finding 7: "Mixed" is undecodable without reading the code. It means the
    // card mixes values editable here with values that come from .env — so the
    // fixture needs a section that genuinely has both.
    const wrapper = await open(undefined, {
      environment: [
        {
          label: 'Webhook variable',
          env_var: 'HOMELABVAULT_DISCORD_WEBHOOK_URL',
          value: null,
          configured: false,
          note: 'Set in .env.',
        },
      ],
    }, 'notifications')
    const mixed = wrapper
      .findAll('.settings-badge')
      .find((el) => el.text() === 'Mixed')
    expect(mixed).toBeTruthy()
    expect(mixed.attributes('title')).toMatch(/editable here/i)
    expect(mixed.attributes('title')).toMatch(/\.env/)
    wrapper.unmount()
  })

  it('every badge carries an explanation, not just the confusing one', async () => {
    const wrapper = await open()
    const badges = wrapper.findAll('.settings-badge')
    expect(badges.length).toBeGreaterThan(0)
    for (const badge of badges) {
      expect(badge.attributes('title')).toBeTruthy()
    }
    wrapper.unmount()
  })

  it('hosts the Export JSON and API links, which left the top bar', async () => {
    // Finding 5: the header was accumulating every link in the app.
    const wrapper = await open(undefined, undefined, 'data-and-api')
    const hrefs = wrapper.findAll('a').map((a) => a.attributes('href'))
    expect(hrefs).toContain('/api/v1/export/json')
    expect(hrefs).toContain('/api/v1/docs')
    wrapper.unmount()
  })
})

describe('the TrueNAS settings card (TN2)', () => {
  // TN2 added the backend TrueNAS section and its TRUENAS_HOST/API_KEY .env
  // rows; TN-WS added the user-linked username as a third credential. Without
  // the ENV_SECTION mapping in this component they fall through to the Access
  // card — the .env-panel half of the settings producer/consumer pair.
  const TRUENAS_FIXTURE = {
    sections: ['Health checks', 'Notifications', 'TrueNAS'],
    environment: [
      {
        label: 'TrueNAS host',
        env_var: 'TRUENAS_HOST',
        status: 'Not configured',
        note: 'The SCALE API endpoint.',
      },
      {
        label: 'TrueNAS API key',
        env_var: 'TRUENAS_API_KEY',
        status: 'Not configured',
        note: 'A read-only key.',
      },
      {
        label: 'TrueNAS API user',
        env_var: 'TRUENAS_API_USER',
        status: 'Not configured',
        note: 'The TrueNAS username the API key is linked to.',
      },
    ],
  }

  it('files the TRUENAS_* .env rows under the TrueNAS card', async () => {
    const wrapper = await open(undefined, TRUENAS_FIXTURE, 'truenas')
    const truenas = wrapper
      .findAll('.settings-card')
      .find((c) => c.find('.settings-card-title').text() === 'TrueNAS')
    expect(truenas).toBeTruthy()
    expect(truenas.text()).toContain('TrueNAS host')
    expect(truenas.text()).toContain('TRUENAS_API_KEY')
    expect(truenas.text()).toContain('TRUENAS_API_USER')
    wrapper.unmount()
  })

  it('keeps them off the Access card', async () => {
    const wrapper = await open(undefined, TRUENAS_FIXTURE, 'access')
    const access = wrapper
      .findAll('.settings-card')
      .find((c) => c.find('.settings-card-title').text() === 'Access')
    expect(access).toBeTruthy()
    expect(access.text()).not.toContain('TRUENAS_HOST')
    expect(access.text()).not.toContain('TRUENAS_API_USER')
    wrapper.unmount()
  })
})

describe('the section rail (TN-SET)', () => {
  it('lists every section plus the links card, and marks the active one', async () => {
    const wrapper = await open(undefined, undefined, 'notifications')
    const items = wrapper.findAll('.settings-rail-item')
    // Backend sections, then the two frontend-owned cards (Access is appended
    // by apply(), the links card by the rail itself).
    expect(items.map((i) => i.text())).toEqual([
      'Health checks',
      'Notifications',
      'Access',
      'Data and API',
    ])
    const active = items.filter((i) => i.classes()).filter((i) => i.classes().includes('active'))
    expect(active).toHaveLength(1)
    expect(active[0].text()).toBe('Notifications')
    wrapper.unmount()
  })

  it('renders only the active section card', async () => {
    const wrapper = await open(undefined, undefined, 'notifications')
    const titles = wrapper
      .findAll('.settings-card-title')
      .map((t) => t.text())
    expect(titles).toEqual(['Notifications'])
    wrapper.unmount()
  })

  it('routes on a rail click rather than keeping tab state', async () => {
    const wrapper = await open()
    const target = wrapper
      .findAll('.settings-rail-item')
      .find((i) => i.text() === 'Notifications')
    await target.trigger('click')
    expect(navigate).toHaveBeenCalledWith('settings', { section: 'notifications' })
    wrapper.unmount()
  })

  it('degrades an unknown section to the first one, like the adapter bar', async () => {
    const wrapper = await open(undefined, undefined, 'nonsense')
    const titles = wrapper.findAll('.settings-card-title').map((t) => t.text())
    expect(titles).toEqual(['Health checks'])
    wrapper.unmount()
  })

  it('marks a section holding unsaved edits while another is on screen', async () => {
    // The savebar is global; the dot is how you find WHERE the edit lives.
    const wrapper = await open(undefined, undefined, 'notifications')
    await wrapper.find('.settings-toggle').trigger('click') // dirty Notifications
    const dirty = wrapper
      .findAll('.settings-rail-item')
      .filter((i) => i.find('.settings-rail-dirty').exists())
    expect(dirty.map((i) => i.text().replace(/\s+$/, ''))).toEqual(['Notifications'])
    wrapper.unmount()
  })

  it('keeps unsaved edits when switching sections — one draft, not per-page state', async () => {
    const wrapper = await open(undefined, undefined, 'notifications')
    await wrapper.find('.settings-toggle').trigger('click')
    // Simulate the route change the rail click causes.
    await wrapper.setProps({ section: 'health-checks' })
    expect(wrapper.text()).toContain('1 unsaved change')
    await wrapper.setProps({ section: 'notifications' })
    expect(wrapper.find('.settings-toggle').classes()).toContain('on')
    wrapper.unmount()
  })
})

describe('the mention ID fields (BACKLOG.1)', () => {
  const MENTION_SPEC = {
    key: 'notifications.discord.mention_user',
    kind: 'discord_mention',
    value: '',
    default: '',
    label: 'Mention user ID',
    helper: 'Discord USER ID to mention on every notification.',
    section: 'Notifications',
  }

  function mentionInput(wrapper) {
    return wrapper
      .findAll('input')
      .find((i) => i.attributes('aria-label') === 'Mention user ID')
  }

  it('renders a real input, not the read-only env_ref fallback', async () => {
    const wrapper = await open(undefined, { settings: [...SPECS, MENTION_SPEC] }, 'notifications')
    const field = mentionInput(wrapper)
    expect(field).toBeTruthy()
    expect(field.attributes('placeholder')).toMatch(/nobody/i)
    wrapper.unmount()
  })

  it('filters to digits as you type — an ID field, never a name field', async () => {
    // The server refuses anything but a snowflake regardless; this is the
    // convenience layer saying the same thing (§6a: the API is the boundary).
    const wrapper = await open(undefined, { settings: [...SPECS, MENTION_SPEC] }, 'notifications')
    const field = mentionInput(wrapper)
    await field.setValue('@everyone123456789012345678')
    expect(field.element.value).toBe('123456789012345678')
    wrapper.unmount()
  })
})

describe('the Access card is one exclusive choice (BACKLOG.4)', () => {
  it('groups the three modes as a radio group with exactly one checked', async () => {
    const wrapper = await open(undefined, { auth_mode: 'open-writes' }, 'access')
    const group = wrapper.find('[role="radiogroup"]')
    expect(group.exists()).toBe(true)

    const radios = group.findAll('[role="radio"]')
    expect(radios.length).toBe(3)
    const checked = radios.filter(
      (r) => r.attributes('aria-checked') === 'true',
    )
    expect(checked.length).toBe(1)
    expect(checked[0].text()).toContain('open-writes')
    wrapper.unmount()
  })

  it('says where the mode is actually set — .env, not this screen', async () => {
    const wrapper = await open(undefined, undefined, 'access')
    const caption = wrapper.find('.settings-modes-caption')
    expect(caption.exists()).toBe(true)
    expect(caption.text()).toContain('HOMELABVAULT_AUTH_MODE')
    expect(caption.text()).toMatch(/one mode/i)
    wrapper.unmount()
  })

  it('draws a radio indicator on every mode row', async () => {
    const wrapper = await open(undefined, undefined, 'access')
    expect(wrapper.findAll('.settings-mode-radio').length).toBe(3)
    wrapper.unmount()
  })
})

describe('the cluster-read failure reason (audit S5)', () => {
  const PROVIDER = {
    name: 'proxmox',
    configured: true,
    usable: true,
    detail: '',
    infrastructure_detail: '',
    guest_count: 6,
    ambiguous_address_count: 0,
    last_polled_at: '2026-08-07T12:00:00Z',
  }

  it('renders the reason the dashboard points at', async () => {
    // The dead end closed: the dashboard says "see Settings for the reason",
    // and this is the reason being visible there.
    const wrapper = await open(undefined, {
      sections: ['Health checks', 'Notifications', 'Proxmox'],
      providers: [
        {
          ...PROVIDER,
          infrastructure_detail:
            'Proxmox answered 403 for /cluster/status. The token may lack Sys.Audit.',
        },
      ],
    }, 'proxmox')
    expect(wrapper.text()).toContain('Cluster panel:')
    expect(wrapper.text()).toContain('Sys.Audit')
    expect(wrapper.text()).toContain('Guest locations keep working')
    wrapper.unmount()
  })

  it('says nothing when the cluster read works', async () => {
    const wrapper = await open(undefined, {
      sections: ['Health checks', 'Notifications', 'Proxmox'],
      providers: [PROVIDER],
    }, 'proxmox')
    expect(wrapper.text()).not.toContain('Cluster panel:')
    wrapper.unmount()
  })
})

describe('saving refreshes the public settings the rest of the app reads', () => {
  // Validation 2026-08-07, finding 3: density (and the landing page) are read
  // from `state.publicSettings`, which was loaded once at mount — so a saved
  // change only showed up after a manual reload. Saving must refresh it.
  it('after save, state.publicSettings carries the new values without a reload', async () => {
    const { state } = await import('../store.js')
    state.publicSettings = {}
    vi.spyOn(api, 'updateSettings').mockResolvedValue(payload())
    vi.spyOn(api, 'getPublicSettings').mockResolvedValue({
      auth_mode: 'full',
      values: { 'ui.catalog.density': 'comfortable' },
    })

    const wrapper = await open(undefined, undefined, 'notifications')
    const toggle = wrapper.find('.settings-toggle')
    await toggle.trigger('click') // dirty the form
    const save = wrapper.findAll('button').find((b) => b.text().includes('Save changes'))
    await save.trigger('click')
    await new Promise((r) => setTimeout(r, 0))

    expect(api.getPublicSettings).toHaveBeenCalled()
    expect(state.publicSettings['ui.catalog.density']).toBe('comfortable')
    state.publicSettings = {}
    wrapper.unmount()
  })
})
