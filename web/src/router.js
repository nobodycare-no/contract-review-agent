import { createRouter, createWebHistory } from 'vue-router'
import Portal from './views/Portal.vue'
import Admin from './views/Admin.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Portal },
    { path: '/admin', component: Admin },
    { path: '/detail/:id', component: () => import('./views/Detail.vue') }
  ]
})
