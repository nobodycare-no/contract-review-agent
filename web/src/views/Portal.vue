<template>
  <div>
    <h1>合同智能审查助手</h1>
    <div class="sub">上传待审合同 → AI 自动解析、按规则库与语义审查 → 生成风险意见。最终决策由审批人做出。</div>

    <section class="card">
      <h3>① 新建审批单（可多选文件批量提交）</h3>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <input v-model="title" placeholder="审批标题（打包模式必填）" style="min-width:230px"/>
        <input v-model="applicant" placeholder="申请人姓名" style="width:140px"/>
        <label><input type="checkbox" v-model="bundle"/> 多附件合并为一张单</label>
        <input type="file" multiple accept=".docx,.pdf,.md,.txt,.png,.jpg,.jpeg" @change="onPick"/>
        <button class="primary" :disabled="uploading" @click="submit">提交</button>
      </div>
      <div :class="upMsg.ok ? 'msg' : 'err'">{{ upMsg.text }}</div>
    </section>

    <div class="bar">
      <span>筛选：</span>
      <select v-model="filter">
        <option value="">全部状态</option>
        <option value="pending">待处理</option>
        <option value="blocked">需人工处理</option>
        <option value="done">已完成</option>
      </select>
      <button @click="pullFromExternal">同步外部待办（可选）</button>
      <label><input type="checkbox" v-model="dry"/> 仅演练，不写入</label>
      <button class="primary" :disabled="!selectedIds.length || running"
              @click="batchStart()">开始批量自动审查（{{ selectedIds.length }}）</button>
      <span class="msg">{{ msg }}</span>
    </div>

    <table>
      <thead><tr><th></th><th>合同 / 审批单</th><th>申请人</th><th>审查状态</th>
        <th>综合风险</th><th>意见写入</th></tr></thead>
      <tbody>
        <tr v-for="t in filtered" :key="t.id">
          <td><input type="checkbox" :value="t.id" v-model="selectedIds"/></td>
          <td><router-link :to="`/detail/${t.id}`" style="color:var(--ac)">{{ t.title }}</router-link></td>
          <td>{{ t.applicant }}</td>
          <td><span :class="'pill p-'+t.task_status">{{ statusZh(t.task_status) }}</span></td>
          <td><span v-if="t.overall_risk_level" :class="'pill lv-'+t.overall_risk_level">{{ LEVEL[t.overall_risk_level] }}</span><span v-else class="pill lv-none">未评估</span></td>
          <td><span :class="'pill p-'+t.write_status">{{ WRITE[t.write_status] }}</span></td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { onMounted, ref, computed } from 'vue'
import { api } from '../api'

const STATUS={pending:'待处理',parsing:'正在解析',reviewing:'正在审查',blocked:'需人工处理',done:'已完成'}
const WRITE ={not_written:'尚未生成',writing:'写入中…',success:'已写入评论区',failed:'写入失败'}
const LEVEL ={high:'高风险',medium:'中风险',low:'低风险'}
const statusZh=s=>STATUS[s]||s

const title=ref(''), applicant=ref('王铁柱'), bundle=ref(false)
const pickedFiles=ref([]), uploading=ref(false)
const upMsg=ref({ok:true,text:''})
const filter=ref(''), rows=ref([]), selectedIds=ref([])
const dry=ref(false), running=ref(false), msg=ref('')

const filtered=computed(()=>filter.value?rows.value.filter(t=>t.task_status===filter.value):rows.value)

function onPick(e){pickedFiles.value=[...e.target.files]}

async function submit(){
  if(!applicant.value){upMsg.value={ok:false,text:'请填写申请人'};return}
  const fd=new FormData()
  fd.append('applicant',applicant.value); fd.append('title',title.value||'')
  fd.append('bundle',String(bundle.value))
  pickedFiles.value.forEach(f=>fd.append('files',f))
  uploading.value=true
  try{const r=await api.createForms(fd);
    upMsg.value=r.ok?{ok:true,text:`已创建 ${r.created.length} 张审批单`}
                    :{ok:false,text:r.errors.map(e=>`${e.file}:${e.reason}`).join('；')}
    await loadQueue();
  }catch(e){upMsg.value={ok:false,text:e.message}}
  uploading.value=false;
}

async function pullFromExternal(){await api.pullForms();loadQueue()}

async function batchStart(){
  running.value=true; msg.value='批次已在后台排队，列表将实时推进…'
  await api.batchReview(selectedIds.value)
  const timer=setInterval(async()=>{
    await loadQueue()
    const active=rows.value.filter(t=>selectedIds.value.includes(t.id)&&
      ['pending','parsing','reviewing'].includes(t.task_status))
    if(!active.length){clearInterval(timer);running.value=false;
      msg.value='批次执行完毕';selectedIds.value=[]}
  },1500)
}

async function loadQueue(){rows.value=(await api.queue()).tasks}
onMounted(()=>{loadQueue();setInterval(loadQueue,3000)})
defineExpose({})
</script>
