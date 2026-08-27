<template>
  <div>
    <h1>运维后台</h1><div class="sub">开发/运维人员视角：任务追踪 · 规则管理 · 系统体检</div>
    <div class="bar" v-if="!authed">
      <input type="password" v-model="tokenIn" placeholder="输入 X-Admin-Token"/>
      <button @click="unlock">进入</button><span class="err">{{ hint }}</span></div>

    <template v-if="authed">
      <div class="bar">
        <span v-for="(c,s) in counts" :key="s">
          <b>{{ s }}</b>: {{ c }}</span>
        <button @click="resetDemo">复位演示数据（清库重种6单）</button>
        <span style="flex:1"></span><a href="/" style="color:var(--ac)">前往前台 →</a></div>

      <table><thead><tr><th>ID</th><th>编号</th><th>标题</th><th>状态</th><th>回写</th><th>instance_id</th></tr></thead>
        <tbody><tr v-for="t in tasks" :key="t.id">
          <td>{{ t.id }}</td><td>{{ t.approval_code }}</td><td>{{ t.title }}</td>
          <td><span :class="'pill p-'+t.task_status">{{ t.task_status }}</span></td>
          <td>{{ t.write_status }}</td><td style="color:var(--dim);font-size:12px">{{ t.instance_id }}</td></tr></tbody></table>

      <section class="card"><h3>规则管理（启停即时生效）</h3>
        <table><thead><tr><th>code</th><th>名称</th><th>级别</th><th>模式</th><th>启用</th></tr></thead>
        <tbody><tr v-for="r in rules" :key="r.rule_code">
          <td>{{ r.rule_code }}</td><td>{{ r.rule_name }}</td>
          <td>{{ r.risk_level }}</td><td>{{ r.match_mode }}</td>
          <td><input type="checkbox" :checked="!!r.rule_status"
              @change="toggle(r,$event.target.checked)"/></td></tr></tbody></table></section>

      <section class="card"><h3>系统指标（/metrics cra_* 摘录）</h3><pre id="m" style="max-height:40vh;overflow:auto;font-size:11px">{{ metricsText }}</pre></section>
    </template>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
const authed=ref(false)
const tokenIn=ref(''),hint=ref(''),tasks=ref([]),rules=ref([]),
      counts=ref({}),metricsText=ref('—')
function unlock(){localStorage.setItem('cra_admin',tokenIn.value);
  if(tokenIn.value){authed.value=true;boot()}else{hint.value='请输入 Token'}}
function hdrs(){return api.adminHeaders()}
async function boot(){
  const [q,rulesResp,healthText]=await Promise.all([
    j('/agent/tasks'), fetch('/admin/rules',{headers:hdrs()}).then(r=>r.json()),
    fetch('/metrics').then(r=>r.text())])
  tasks.value=q.tasks; rules.value=rulesResp.rules
  counts.value=(await api.queue()).counts
  metricsText.value=healthText.split('\n').filter(l=>l.startsWith('cra_')).join('\n')
}
async function toggle(r,on){
  await fetch(`/admin/rules/${r.rule_code}`,{method:'PUT',
    headers:{...hdrs(),'Content-Type':'application/json'},
    body:JSON.stringify({rule_status:on?1:0})})
  await boot()}
async function resetDemo(){
  await fetch('/admin/reset-demo',{method:'POST',headers:hdrs()})
  boot()}
const j=u=>fetch(u).then(r=>r.json())
onMounted(()=>{if(localStorage.getItem('cra_admin')){authed.value=true;boot()}})
</script>
