import { config } from '@vue/test-utils'
import ui from '@nuxt/ui/vue-plugin'
import { vi } from 'vitest'

config.global.plugins.push(ui)
config.global.stubs.UDropdownMenu = { template: '<div><slot /></div>' }
config.global.stubs.UTooltip = { template: '<slot />' }
config.global.stubs.UIcon = { template: '<span aria-hidden="true" />' }
// Layout primitives are tested with real components in Playwright. Unit tests exercise their slots.
config.global.stubs.UDashboardGroup = { template: '<div><slot /></div>' }
config.global.stubs.UDashboardSidebar = { template: '<aside><slot /></aside>' }
config.global.stubs.UDashboardPanel = {
  template: '<div><slot name="header" /><slot name="body" /><slot /></div>',
}
config.global.stubs.UDashboardNavbar = {
  props: ['title'],
  template:
    '<header>{{ title }}<slot name="leading" /><slot name="title" /><slot name="right" /></header>',
}
config.global.stubs.UDashboardSidebarToggle = { template: '<button aria-label="打开导航" />' }
window.matchMedia = vi.fn().mockImplementation((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener: vi.fn(),
  removeListener: vi.fn(),
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
  dispatchEvent: vi.fn(),
}))
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

for (const [name, stub] of Object.entries(config.global.stubs)) {
  if (name.startsWith('U')) config.global.stubs[name.slice(1)] = stub
}
