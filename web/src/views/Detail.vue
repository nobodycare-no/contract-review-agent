<template>
  <div v-if="d">
    <div class="bar"><a class="back" @click="$router.back()">← 返回</a>
      <button @click="load">刷新</button>
      <button v-if="d.task.task_status==='done'" class="primary"
              :disabled="rerunning" @click="doRerun">再次审查</button>
      <span v-if="d.review" style="font-size:18px;font-weight:700">
        综合风险：{{ LEVEL[d.review.overall_risk_level]||d.review.overall_risk_level }}</span>
      <span class="msg">{{ rerunHint }}</span></div>

    <section class="card"><h3>原始合同文件</h3>
     <div class="kv"><template v-for="a in d.attachments||[]" :key="a.attachment_id">
       <b>{{ a.file_name }}</b>
       <span>{{ a.download_status==='done' ? '' : '缺失' }}
         <a v-if="a.download_status==='done'" style="color:var(--ac);cursor:pointer;margin-left:8px"
            :href="`/app/files/${d.task.id}/${a.attachment_id}`" target="_blank">查看原件 ↗</a></span></template>
       <b v-if="!(d.attachments||[]).length">-</b><span v-if="!(d.attachments||[]).length" style="color:var(--dim)">无附件</span></div></section>
    <section class="card"><h3>合同基本信息</h3><div class="kv">
      <template v-for="(v,k) in d.parse?.basic_info||{}" :key="k">
        <b>{{ ZH[k]||k }}</b><span>{{ v.value ?? '—' }}<i v-if="v.status==='inferred'">（推断）</i></span>
      </template></div></section>

    <section class="card"><h3>按规则识别的风险</h3>
      <div v-for="h in ruleHits" :key="h.rule_id" class="issue"
           :class="h.risk_level==='high'?'h':h.risk_level==='medium'?'m':''">
        <div style="font-weight:600">{{ LEVEL[h.risk_level] }} · {{ h.rule_name }}</div>
        <div v-if="h.evidence" style="color:#59637d;font-size:13px">📄 <em>{{ h.evidence.slice(0,120) }}</em></div>
      </div>
      <i v-if="!ruleHits.length" style="color:var(--dim)">规则库全部通过 ✓</i></section>

    <section v-if="aiHits.length" class="card"><h3>AI 补充提示（供参考）</h3>
      <div v-for="h in aiHits" :key="h.rule_id" class="issue m">
        {{ h.rule_name }}
        <div style="font-size:13px;color:#59637d">{{ h.evidence }}</div></div></section>

    <section v-if="d.review" class="card"><h3>审查意见全文</h3><pre class="opinion">{{ d.review.comment_text }}</pre></section>
    <section class="card"><h3>状态</h3><div class="kv"><b>任务</b><span>{{ STATUS[d.task.task_status] }}</span>
      <b>意见写入</b><span>{{ WRITE[d.task.write_status] }}</span>
      <template v-if="d.task.block_reason"><b>阻塞原因</b><span style="color:var(--hi)">{{ d.task.block_reason }}</span>
        <b></b><button @click="doRetry">重新处理此单</button>
        <span class="err" style="font-size:12px">{{ retryHint }}</span></template></div></section>

    <section class="card"><h3>审查留痕</h3>
      <div v-if="!timeline.length" style="color:var(--dim);font-size:13px">
        尚无留痕记录。运行一次审查后，这里会记录每一次工具调用、状态迁移与意见写入。</div>
      <div class="tl">
        <div v-for="(e,i) in timeline" :key="i" class="tl-item">
          <span class="tl-dot" :class="'tl-'+e.kind"></span>
          <div style="min-width:0">
            <div style="font-size:13px"><b style="color:var(--dim)">{{ e.time }}</b>
              <span style="margin-left:6px">{{ e.text }}</span></div>
            <div v-if="e.detail" class="tl-detail">{{ e.detail }}</div>
          </div>
        </div>
      </div></section>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
const d=ref(null), logs=ref([])
const LEVEL={high:'高风险',medium:'中风险',low:'低风险'}
const STATUS={pending:'待处理',parsing:'正在解析',reviewing:'正在审查',blocked:'需人工处理',done:'已完成'}
const WRITE={not_written:'尚未生成',writing:'写入中…',success:'已写入评论区',failed:'写入失败'}
const LOG_ZH={tool:'工具调用',agent:'Agent 动作',write:'意见写入',system:'系统',transition:'状态迁移'}
const ZH={contract_title:'合同名称',contract_no:'合同编号',party_a:'甲方(己方)',party_b:'乙方(对方)',
          amount:'合同金额',currency:'币种',effective_date:'生效日期',expire_date:'到期日期'}
const route=useRoute()
const hits=computed(()=>d.value?.hits?.filter(h=>h.hit_status==='hit')||[])
const ruleHits=computed(()=>hits.value.filter(h=>h.rule_code!=='AI_DISCRETIONARY'))
const aiHits=computed(()=>hits.value.filter(h=>h.rule_code==='AI_DISCRETIONARY'))

const timeline=computed(()=>{
  const items=[]
  for(const c of d.value?.comment_logs||[])
    items.push({time:String(c.created_at), kind:'write',
      text:c.write_status==='success' ? '审查意见已写入审批单评论区'
         : '写入动作：'+(WRITE[c.write_status]||c.write_status)})
  for(const l of logs.value||[])
    items.push({time:String(l.created_at), kind:l.level==='error'?'err':'tool',
      text:(LOG_ZH[l.type]||l.type)+' · '+l.content})
  return items.sort((a,b)=>a.time<b.time?1:-1)
})

async function load(){
  d.value=await api.detail(route.params.id)
  logs.value=await api.taskLogs(route.params.id).then(r=>r.logs||[]).catch(()=>[])
}
let retryHint=ref('')
async function doRetry(){const r=await api.retry(route.params.id).catch(e=>({resumed_stage:'FAIL',detail:e.message}));if(r.resumed_stage){retryHint.value='';load()}else{retryHint.value=r.detail||'无法重试——请先补传附件';}}

const rerunning=ref(false), rerunHint=ref('')
async function doRerun(){
  rerunning.value=true
  rerunHint.value='AI 正在重新审查（真机推理约需 30~60 秒），完成后自动刷新…'
  const r=await api.runAgent({task_id:d.value.task.id, dry_run:false}).catch(e=>({detail:e.message}))
  rerunning.value=false
  if(r&&r.status==='succeeded'){rerunHint.value='';load()}
  else{rerunHint.value=(r&&r.detail)||'重审失败，请稍后重试'}
}
load()
</script>

<style scoped>
.tl{position:relative;margin:6px 0 0 6px;padding-left:20px;border-left:2px solid rgba(148,163,184,.25)}
.tl-item{position:relative;margin-bottom:14px}
.tl-dot{position:absolute;left:-27px;top:4px;width:10px;height:10px;border-radius:50%;
  background:var(--ac,#5b8cff);box-shadow:0 0 0 3px rgba(91,140,255,.15)}
.tl-dot.tl-err{background:var(--hi,#e5484d);box-shadow:0 0 0 3px rgba(229,72,77,.15)}
.tl-dot.tl-write{background:#2ea36c;box-shadow:0 0 0 3px rgba(46,163,108,.15)}
.tl-detail{font-size:12px;color:var(--dim,#8b93a7);margin-top:2px;word-break:break-all}
</style>
