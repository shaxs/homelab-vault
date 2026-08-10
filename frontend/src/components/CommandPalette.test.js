import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { state } from '../store.js'
import CommandPalette from './CommandPalette.vue'

const SERVICES = [
  { slug: 'jellyfin', name: 'Jellyfin', category: 'media', launch_url: 'https://j/' },
  { slug: 'sonarr', name: 'Sonarr', category: 'media', launch_url: 'https://s/' },
]

beforeEach(() => {
  state.services = [...SERVICES]
  state.views = []
  window.open = vi.fn()
})

function open() {
  return mount(CommandPalette, { attachTo: document.body })
}

describe('CommandPalette', () => {
  it('closes on Escape', async () => {
    const wrapper = open()

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()

    expect(wrapper.emitted('close')).toBeTruthy()
    wrapper.unmount()
  })

  it('closes on Escape typed into its own input', async () => {
    // The palette autofocuses its search box, so this is the only path that
    // actually matters in use.
    const wrapper = open()
    const input = wrapper.find('input').element
    input.focus()

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    )
    await wrapper.vm.$nextTick()

    expect(wrapper.emitted('close')).toBeTruthy()
    wrapper.unmount()
  })

  it('closes on Escape without relying on the event reaching document', async () => {
    // Belt and braces: a browser autocomplete popup can swallow the first
    // Escape before it bubbles, so the input handles it directly too.
    const wrapper = open()

    await wrapper.find('input').trigger('keydown.esc')

    expect(wrapper.emitted('close')).toBeTruthy()
    wrapper.unmount()
  })

  it('Enter opens the record rather than launching', async () => {
    // Ruling 2026-08-03: §5's "named tab vs new tab" split was unobservable in
    // a real browser, so Enter was given a distinct, visible job.
    const wrapper = open()

    await wrapper.find('input').trigger('keydown', { key: 'Enter' })

    expect(wrapper.emitted('open-record')[0][0].slug).toBe('jellyfin')
    expect(window.open).not.toHaveBeenCalled()
    expect(wrapper.emitted('close')).toBeTruthy()
    wrapper.unmount()
  })

  it('Cmd+Enter launches the service instead of opening the record', async () => {
    const wrapper = open()

    await wrapper.find('input').trigger('keydown', { key: 'Enter', metaKey: true })

    expect(window.open).toHaveBeenCalledWith('https://j/', '_blank', 'noopener')
    expect(wrapper.emitted('open-record')).toBeFalsy()
    wrapper.unmount()
  })

  it('Ctrl+Enter launches too, for non-mac keyboards', async () => {
    const wrapper = open()

    await wrapper.find('input').trigger('keydown', { key: 'Enter', ctrlKey: true })

    expect(window.open).toHaveBeenCalledWith('https://j/', '_blank', 'noopener')
    wrapper.unmount()
  })

  it('arrow keys move the selection', async () => {
    const wrapper = open()
    const input = wrapper.find('input')

    await input.trigger('keydown', { key: 'ArrowDown' })
    await input.trigger('keydown', { key: 'Enter' })

    expect(wrapper.emitted('open-record')[0][0].slug).toBe('sonarr')
    wrapper.unmount()
  })

  it('a saved view still applies its query on Enter', async () => {
    state.views = [{ id: 'v1', name: 'Broken', query: 'state:down' }]
    const wrapper = mount(CommandPalette, { attachTo: document.body })

    await wrapper.find('input').setValue('Broken')
    await wrapper.find('input').trigger('keydown', { key: 'Enter' })

    expect(wrapper.emitted('run-view')[0][0]).toBe('state:down')
    wrapper.unmount()
  })
})
