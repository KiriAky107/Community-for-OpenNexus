import { createApp } from 'vue'
import App from './App.vue'
import Workbench from './Workbench.vue'
import { initializeLocale } from './locale.ts'
import './style.css'
import './workbench.css'

initializeLocale()
createApp(location.pathname === '/workbench' ? Workbench : App).mount('#app')
