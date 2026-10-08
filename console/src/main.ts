import { createApp } from 'vue'
import App from './App.vue'
import Workbench from './Workbench.vue'
import './style.css'
import './workbench.css'

createApp(location.pathname === '/workbench' ? Workbench : App).mount('#app')
