import { createRouter, createWebHashHistory } from 'vue-router'
import Portal from './views/Portal.vue'
import Admin from './views/Admin.vue'

export default createRouter({
  // 哈希模式：纯静态托管下任意深链(如 /#/admin)都稳定可达，无需服务端重写
  history: createWebHashHistory(),
  routes: [
    { path: '/', component: Portal },
    { path: '/admin', component: Admin },
    { path: '/detail/:id', component: () => import('./views/Detail.vue') }
  ]
})
