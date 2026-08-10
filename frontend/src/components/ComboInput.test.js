/**
 * The in-app suggestion list that replaced the native `<datalist>`.
 *
 * The native control was rejected for looking like browser chrome and for having
 * no affordance — it only opened once you had typed. So the two behaviours worth
 * pinning are **it opens on focus** (that is the discoverability the old one
 * lacked) and **the typed value always wins** (category is free text; the list
 * exists to stop `Media` being reinvented as `media`, not to constrain input).
 *
 * The mousedown-not-click detail is the one that would silently break: `blur`
 * fires before `click`, so a list that closed on blur would unmount the option
 * before a click could land, and picking with the mouse would do nothing at all.
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ComboInput from './ComboInput.vue'

const SUGGESTIONS = ['AI', 'Infrastructure', 'Media', 'Productivity', 'Utilities']

function open(props = {}) {
  const wrapper = mount(ComboInput, {
    props: { modelValue: '', suggestions: SUGGESTIONS, ...props },
  })
  return wrapper
}

const options = (wrapper) => wrapper.findAll('.combo-option').map((o) => o.text())

describe('opening', () => {
  it('shows nothing until the field is focused', () => {
    const wrapper = open()
    expect(wrapper.find('.combo-list').exists()).toBe(false)
  })

  it('shows every suggestion on focus, before anything is typed', async () => {
    // The whole point: the native datalist only opened once you had typed, so
    // you could not discover what already existed.
    const wrapper = open()
    await wrapper.find('input').trigger('focus')
    expect(options(wrapper)).toEqual(SUGGESTIONS)
  })

  it('closes on blur', async () => {
    const wrapper = open()
    await wrapper.find('input').trigger('focus')
    await wrapper.find('input').trigger('blur')
    expect(wrapper.find('.combo-list').exists()).toBe(false)
  })
})

describe('filtering', () => {
  it('narrows case-insensitively on a substring', async () => {
    const wrapper = open({ modelValue: 'ut' })
    await wrapper.find('input').trigger('focus')
    expect(options(wrapper)).toEqual(['Utilities'])
  })

  it('matches mid-word, not just as a prefix', async () => {
    const wrapper = open({ modelValue: 'struct' })
    await wrapper.find('input').trigger('focus')
    expect(options(wrapper)).toEqual(['Infrastructure'])
  })

  it('drops an exact match, which would only ever be noise', async () => {
    const wrapper = open({ modelValue: 'Media' })
    await wrapper.find('input').trigger('focus')
    expect(options(wrapper)).not.toContain('Media')
  })

  it('renders no list when nothing matches, rather than an empty box', async () => {
    const wrapper = open({ modelValue: 'zzz' })
    await wrapper.find('input').trigger('focus')
    expect(wrapper.find('.combo-list').exists()).toBe(false)
  })
})

describe('picking', () => {
  it('commits on mousedown, because blur beats click', async () => {
    // If this ever moves to @click, picking with the mouse silently stops
    // working: blur closes the list and unmounts the row first.
    const wrapper = open()
    await wrapper.find('input').trigger('focus')
    await wrapper.findAll('.combo-option')[2].trigger('mousedown')
    expect(wrapper.emitted('update:modelValue').at(-1)).toEqual(['Media'])
  })

  it('closes the list once a value is chosen', async () => {
    const wrapper = open()
    await wrapper.find('input').trigger('focus')
    await wrapper.findAll('.combo-option')[0].trigger('mousedown')
    expect(wrapper.find('.combo-list').exists()).toBe(false)
  })

  it('lets a typed value through unchanged — this is free text', async () => {
    const wrapper = open()
    const input = wrapper.find('input')
    await input.setValue('Something New')
    expect(wrapper.emitted('update:modelValue').at(-1)).toEqual(['Something New'])
  })
})

describe('the keyboard', () => {
  const activeIndex = (wrapper) =>
    wrapper.findAll('.combo-option').findIndex((o) => o.classes().includes('active'))

  it('walks the list with the arrow keys', async () => {
    // The first ↓ must land on row 0. It previously did nothing, because the
    // wrap arithmetic was off by one around the -1 "nothing highlighted" state.
    const wrapper = open()
    const input = wrapper.find('input')
    await input.trigger('focus')
    await input.trigger('keydown', { key: 'ArrowDown' })
    expect(activeIndex(wrapper)).toBe(0)
    await input.trigger('keydown', { key: 'ArrowDown' })
    expect(activeIndex(wrapper)).toBe(1)
    await input.trigger('keydown', { key: 'ArrowUp' })
    expect(activeIndex(wrapper)).toBe(0)
  })

  it('wraps back to nothing highlighted, so ↑ never traps you', async () => {
    const wrapper = open()
    const input = wrapper.find('input')
    await input.trigger('focus')
    await input.trigger('keydown', { key: 'ArrowDown' })
    await input.trigger('keydown', { key: 'ArrowUp' })
    expect(activeIndex(wrapper)).toBe(-1)
  })

  it('↑ from nothing highlighted jumps to the last row', async () => {
    const wrapper = open()
    const input = wrapper.find('input')
    await input.trigger('focus')
    await input.trigger('keydown', { key: 'ArrowUp' })
    expect(activeIndex(wrapper)).toBe(SUGGESTIONS.length - 1)
  })

  it('commits the highlighted option on Enter', async () => {
    const wrapper = open()
    const input = wrapper.find('input')
    await input.trigger('focus')
    await input.trigger('keydown', { key: 'ArrowDown' })
    await input.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('update:modelValue').at(-1)).toEqual(['AI'])
  })

  it('leaves Enter alone when the list is open but nothing is highlighted', async () => {
    // Otherwise the combobox swallows the keypress that submits the form it is
    // sitting in — and worse, emits `undefined` as the value, because there is
    // no row at index -1.
    //
    // The list must genuinely be OPEN for this to test anything: a value that
    // matches nothing closes the list and the guard is never reached.
    const wrapper = open({ modelValue: 'M' })
    const input = wrapper.find('input')
    await input.trigger('focus')
    expect(wrapper.find('.combo-list').exists()).toBe(true)
    await input.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('closes on Escape without discarding what was typed', async () => {
    const wrapper = open({ modelValue: 'Med' })
    const input = wrapper.find('input')
    await input.trigger('focus')
    await input.trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('.combo-list').exists()).toBe(false)
    // The value is the user's; closing a list must not edit it.
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(input.element.value).toBe('Med')
  })
})

describe('accessibility wiring', () => {
  it('announces itself as a combobox and tracks expansion', async () => {
    const wrapper = open()
    const input = wrapper.find('input')
    expect(input.attributes('role')).toBe('combobox')
    expect(input.attributes('aria-expanded')).toBe('false')
    await input.trigger('focus')
    expect(input.attributes('aria-expanded')).toBe('true')
  })

  it('turns the browser’s own autofill off, so only one list can appear', () => {
    expect(open().find('input').attributes('autocomplete')).toBe('off')
  })
})

describe('the empty case', () => {
  it('offers a hint when there is nothing to suggest yet', async () => {
    const wrapper = open({
      suggestions: [],
      emptyHint: 'No categories yet — the first one you type becomes a suggestion.',
    })
    await wrapper.find('input').trigger('focus')
    expect(wrapper.find('.combo-empty').text()).toContain('No categories yet')
  })

  it('stays silent when no hint was supplied', async () => {
    const wrapper = open({ suggestions: [] })
    await wrapper.find('input').trigger('focus')
    expect(wrapper.find('.combo-empty').exists()).toBe(false)
  })
})
