import { createSSRApp } from 'vue'
import App from './App.vue'
import { installH5KeyboardButtons } from './shared/platform/keyboardButtons'

// #ifdef H5
installH5KeyboardButtons()
// #endif

export function createApp() {
  const app = createSSRApp(App)
  return { app }
}
