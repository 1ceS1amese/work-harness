import { createApp } from 'vue'
import ui from '@nuxt/ui/vue-plugin'
import { zh_cn } from '@nuxt/ui/locale'
import UApp from '@nuxt/ui/components/App.vue'
import { h } from 'vue'
import App from './App.vue'
import './styles.css'

createApp({
  render: () => h(UApp, { locale: zh_cn, tooltip: { delayDuration: 300 } }, () => h(App)),
})
  .use(ui)
  .mount('#app')
