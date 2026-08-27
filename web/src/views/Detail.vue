<template>
  <div v-if="d">
    <div class="bar"><a class="back" @click="$router.back()">← 返回</a>
      <button @click="load">刷新</button>
      <span v-if="d.review" style="font-size:18px;font-weight:700">
        综合风险：{{ LEVEL[d.review.overall_risk_level]||d.review.overall_risk_level }}</span></div>

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
        <b></b><button @click="doRetry">重新处理此单</button></template></div></section>

    <section class="card"><h3>审查留痕（写入记录时间线）</h3>
      <div v-if="!d.comment_logs.length" style="color:var(--dim);font-size:13px">
        尚无写入动作。上方「审查意见全文」即未写入时的最终文案。</div>
      <div v-for="(c,i) in d.comment_logs" :key="i" style="margin-bottom:6px;font-size:13px">
        {{ i+1 }}. <b>{{ WRITE[c.write_status]||c.write_status }}</b>
        <span v-if="c.response" style="color:var(--dim)">· {{ c.response }}</span></div></section>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
const d=ref(null)
const LEVEL={high:'高风险',medium:'中风险',low:'低风险'}
const STATUS={pending:'待处理',parsing:'正在解析',reviewing:'正在审查',blocked:'需人工处理',done:'已完成'}
const WRITE={not_written:'尚未生成',writing:'写入中…',success:'已写入评论区',failed:'写入失败'}
const ZH={contract_title:'合同名称',contract_no:'合同编号',party_a:'甲方(己方)',party_b:'乙方(对方)',
          amount:'合同金额',currency:'币种',effective_date:'生效日期',expire_date:'到期日期'}
const route=useRoute()
const hits=computed(()=>d.value?.hits?.filter(h=>h.hit_status==='hit')||[])
const ruleHits=computed(()=>hits.value.filter(h=>h.rule_code!=='AI_DISCRETIONARY'))
const aiHits=computed(()=>hits.value.filter(h=>h.rule_code==='AI_DISCRETIONARY'))
async function load(){d.value=await api.detail(route.params.id)}
async function doRetry(){await api.retry(route.params.id);load()}
load()
</script>
