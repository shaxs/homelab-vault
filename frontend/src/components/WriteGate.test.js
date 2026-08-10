import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api.js'
import WriteGate from './WriteGate.vue'

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('WriteGate', () => {
  it('sends the token trimmed — a pasted newline is not part of the credential', async () => {
    // The emptiness check has always trimmed; the request did not (audit
    // informational), so a token pasted with a trailing newline failed with
    // an opaque "Not authorised." and no hint at what was wrong.
    const spy = vi.spyOn(api, 'startSession').mockResolvedValue({})
    const wrapper = mount(WriteGate)

    await wrapper.find('input').setValue('  secret-token\n')
    await wrapper.find('.btn-primary').trigger('click')

    expect(spy).toHaveBeenCalledWith('secret-token')
    wrapper.unmount()
  })

  it('still refuses a whitespace-only token without a request', async () => {
    const spy = vi.spyOn(api, 'startSession').mockResolvedValue({})
    const wrapper = mount(WriteGate)

    await wrapper.find('input').setValue('   ')
    await wrapper.find('.btn-primary').trigger('click')

    expect(spy).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('Enter the admin token.')
    wrapper.unmount()
  })
})
