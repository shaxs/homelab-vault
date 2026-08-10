import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import BaseModal from './BaseModal.vue'

function pressEscape() {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
}

describe('BaseModal', () => {
  it('closes on Escape', async () => {
    const wrapper = mount(BaseModal, {
      props: { title: 'Test' },
      attachTo: document.body,
    })

    pressEscape()
    await wrapper.vm.$nextTick()

    expect(wrapper.emitted('close')).toBeTruthy()
    wrapper.unmount()
  })

  it('closes on Escape while focus is inside an input', async () => {
    // The realistic case: every modal in the app autofocuses a field, so the
    // key event originates from the input rather than from document.
    const wrapper = mount(BaseModal, {
      props: { title: 'Test' },
      slots: { default: '<input id="inner" />' },
      attachTo: document.body,
    })
    const input = wrapper.find('#inner').element
    input.focus()

    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    )
    await wrapper.vm.$nextTick()

    expect(wrapper.emitted('close')).toBeTruthy()
    wrapper.unmount()
  })

  it('stops listening once unmounted', async () => {
    const wrapper = mount(BaseModal, { attachTo: document.body })
    wrapper.unmount()

    pressEscape()

    expect(wrapper.emitted('close')).toBeFalsy()
  })

  it('closes when the backdrop itself is clicked', async () => {
    const wrapper = mount(BaseModal, { attachTo: document.body })

    await wrapper.find('.modal-backdrop').trigger('mousedown')

    expect(wrapper.emitted('close')).toBeTruthy()
    wrapper.unmount()
  })

  it('sends Escape only to the top of a stack of modals', async () => {
    // The real pairing: the write gate opens over a filled AddService form.
    // Before the stack existed, one Escape dismissed the gate AND destroyed
    // the form beneath it with everything typed (audit S10).
    const bottom = mount(BaseModal, { props: { title: 'Form' }, attachTo: document.body })
    const top = mount(BaseModal, { props: { title: 'Gate' }, attachTo: document.body })

    pressEscape()
    await top.vm.$nextTick()

    expect(top.emitted('close')).toBeTruthy()
    expect(bottom.emitted('close')).toBeFalsy()

    // With the top gone, Escape belongs to the survivor again.
    top.unmount()
    pressEscape()
    await bottom.vm.$nextTick()

    expect(bottom.emitted('close')).toBeTruthy()
    bottom.unmount()
  })

  it('keeps the stack straight when modals unmount out of order', async () => {
    const first = mount(BaseModal, { attachTo: document.body })
    const second = mount(BaseModal, { attachTo: document.body })

    // The BOTTOM modal leaves first (its v-if drops while the top one shows).
    first.unmount()
    pressEscape()
    await second.vm.$nextTick()

    expect(second.emitted('close')).toBeTruthy()
    second.unmount()
  })
})
