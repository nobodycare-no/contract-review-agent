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
        <option value="queued">排队中</option>
        <option value="blocked">需人工处理</option>
        <option value="done">已完成</option>
      </select>
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
          <td><span :class="'pill ' + pillCls(t)" :title="t.block_reason||''">{{ statusZh(t.task_status) }}</span>
            <button v-if="['pending','blocked'].includes(t.task_status)" class="rowRun"
                    :disabled="busyId===t.id || running"
                    @click="runOne(t)">{{ busyId===t.id ? '审查中…' : (t.task_status==='blocked' ? '重试' : '审查') }}</button></td>
          <td><span v-if="t.overall_risk_level" :class="'pill lv-'+riskCls(t)">{{ LEVEL[t.overall_risk_level]||t.overall_risk_level }}</span><span v-else class="pill lv-none">未评估</span></td>
          <td><span :class="'pill p-'+t.write_status">{{ WRITE[t.write_status] }}</span></td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { onMounted, ref, computed } from 'vue'
import { api } from '../api'

const STATUS={pending:'待处理',queued:'排队中',parsing:'AI 审查中',reviewing:'AI 审查中',blocked:'需人工处理',done:'已完成'}
const WRITE ={not_written:'尚未生成',writing:'写入中…',success:'已写入评论区',failed:'写入失败'}
const LEVEL ={high:'高风险',medium:'中风险',low:'低风险',高:'高风险',中:'中风险',低:'低风险'}
const RISK_CLS={high:'high',medium:'medium',low:'low',高:'high',中:'medium',低:'low'}
const statusZh=s=>STATUS[s]||s
// parsing/reviewing 是内部工程状态（自愈锚点/CAS锁），对用户统一呈现为「AI 审查中」
const pillCls=t=>['parsing','reviewing'].includes(t.task_status)?'p-reviewing':'p-'+t.task_status
const riskCls=t=>RISK_CLS[t.overall_risk_level]||t.overall_risk_level

const title=ref(''), applicant=ref('王铁柱'), bundle=ref(false)
const pickedFiles=ref([]), uploading=ref(false)
const upMsg=ref({ok:true,text:''})
const filter=ref(''), rows=ref([]), selectedIds=ref([])
const running=ref(false), msg=ref('')

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
    upMsg.value=r.ok?{ok:true,text:`已创建 ${r.created.length} 张待审查单——勾选后点「批量自动审查」，或点行内「审查」单张即跑`}
                    :{ok:false,text:r.errors.map(e=>`${e.file}:${e.reason}`).join('；')}
    await loadQueue();
  }catch(e){upMsg.value={ok:false,text:e.message}}
  uploading.value=false;
}


const BKEY='cra_active_batch'

function watchBatch(bid){
  running.value=true
  msg.value='后台批次处理中：已排队合同由工人并行审查（每张约 30~60 秒）。本页刷新不丢失跟踪。'
  let miss=0   // 连续拿不到账本的次数（网络抖动容忍，服务重启解锁）
  const safety=setTimeout(()=>{
    if(running.value){running.value=false; localStorage.removeItem(BKEY)
      msg.value='批次等待超时——请以列表状态与留痕为准'}
  },2400000)
  const timer=setInterval(async()=>{
    loadQueue()
    const st=await api.batchStatus(bid).catch(()=>null)
    if(st===null){ miss+=1
      if(miss>=5){ clearInterval(timer);clearTimeout(safety);running.value=false
        localStorage.removeItem(BKEY)
        msg.value='批次记录已不存在（服务可能重启）——请以列表状态为准，可重新发起审查'}
      return }
    miss=0
    if(st&&(st.done+st.skipped+(st.failed||0))>=st.total){
      clearInterval(timer);clearTimeout(safety);running.value=false
      localStorage.removeItem(BKEY)
      msg.value=`批次执行完毕：成功 ${st.done} 张${st.failed?`，失败 ${st.failed} 张（已转需人工处理）`:''}${st.skipped?`，跳过 ${st.skipped} 张忙单`:''}`
      selectedIds.value=[]
    }
  },1500)
}

async function batchStart(){
  running.value=true
  msg.value='已受理：选中合同全部标记「排队中」，后台并行审查。本页刷新不丢失跟踪。'
  const r=await api.batchReview(selectedIds.value)
  if(!r.batch_id){running.value=false; msg.value=r.detail||'提交失败'; return}
  localStorage.setItem(BKEY, JSON.stringify({bid:r.batch_id, ts:Date.now()}))
  selectedIds.value=[]
  watchBatch(r.batch_id)
  loadQueue()   // 立刻拉一次列表——排队徽标马上可见，不等 3s 轮询
}

const busyId=ref(null)
async function runOne(t){
  busyId.value=t.id
  const r=await api.runAgent({task_id:t.id, dry_run:false}).catch(e=>({detail:e.message}))
  busyId.value=null
  if(r&&r.status==='succeeded'){
    msg.value=`「${t.title}」审查完成（服务端 ${((r.elapsed_ms||0)/1000).toFixed(1)}s）`
    loadQueue()
  }else{
    msg.value=(r&&r.detail)||'运行失败——该单已转「需人工处理」'
    loadQueue()
  }
}

async function loadQueue(){rows.value=(await api.queue()).tasks}
onMounted(()=>{
  loadQueue(); setInterval(loadQueue,3000)
  const saved=JSON.parse(localStorage.getItem(BKEY)||'null')
  if(saved && saved.bid && Date.now()-saved.ts < 15*60*1000){
    watchBatch(saved.bid)   // 刷新/跳转后恢复对未完成批次的跟踪
  }else{
    localStorage.removeItem(BKEY)
  }
})
defineExpose({})
</script>

<style scoped>
.rowRun{margin-left:8px;font-size:12px;padding:2px 10px;cursor:pointer}
.rowRun:disabled{opacity:.5;cursor:default}
</style>
