<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import * as api from '../api/usage'
const data = ref<api.UsageOverview | null>(null), error = ref(''), busy = ref(false)
const userId = ref<number | undefined>()
let disposed = false
const money = (value: number) => (value / 1_000_000).toFixed(4)
const state: Record<string,string> = { running: '生成中', completed: '完成', failed: '失败', cancelled: '已停止', interrupted: '已中断' }
async function refresh() {
  try { const result = await api.overview(); if (!disposed) { data.value = result; error.value = '' } }
  catch (e) { if (!disposed) error.value = e instanceof Error ? e.message : '用量读取失败' }
}
async function toggle(paused: boolean, owner?: number) {
  if (busy.value) return
  busy.value = true
  try { await api.pause(paused, owner); if (!disposed) await refresh() }
  catch (e) { if (!disposed) error.value = e instanceof Error ? e.message : '设置失败' }
  finally { if (!disposed) busy.value = false }
}
onMounted(refresh); onBeforeUnmount(() => { disposed = true })
</script>
<template>
  <section><h3>模型用量与额度</h3>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="data">
      <p>今日提问 {{ data.global_day.questions }}/{{ data.limits.global_daily_questions }}；估算费用 ¥{{ money(data.global_day.spent_micros) }}，预占 ¥{{ money(data.global_day.reserved_micros) }}，日预算 ¥{{ money(data.global_day.budget_micros) }}。</p>
      <p>本月估算费用 ¥{{ money(data.global_month.spent_micros) }}，月预算 ¥{{ money(data.global_month.budget_micros) }}。</p>
      <small>采用版本化高峰价保守估算，不是供应商账单。未知用量按预占金额记入；额度配置见后端 .env。</small>
      <p class="usage-actions"><button :disabled="busy" @click="refresh">刷新用量</button> <button :disabled="busy" @click="toggle(!data.global_paused)">{{ data.global_paused ? '恢复全站新模型调用' : '暂停全站新模型调用' }}</button></p>
      <div class="usage-actions"><label>账号ID <input v-model.number="userId" type="number" min="1" step="1"></label>
        <button :disabled="busy || !userId" @click="toggle(true, userId)">暂停该账号</button>
        <button :disabled="busy || !userId" @click="toggle(false, userId)">恢复该账号</button>
      </div>
      <h4>今日账号用量</h4>
      <div style="overflow-x:auto"><table><thead><tr><th>用户</th><th>提问次数</th><th>估算费用</th><th>预占</th><th>状态</th></tr></thead>
        <tbody><tr v-for="user in data.users" :key="user.owner_id"><td>{{ user.username }}（{{ user.owner_id }}）</td><td>{{ user.questions }}</td><td>¥{{ money(user.spent_micros) }}</td><td>¥{{ money(user.reserved_micros) }}</td><td>{{ user.paused ? '暂停' : '可用' }}</td></tr></tbody>
      </table></div>
      <h4>最近50次请求</h4>
      <div style="overflow-x:auto"><table><thead><tr><th>用户ID</th><th>状态</th><th>估算费用</th><th>待核对调用</th></tr></thead>
        <tbody><tr v-for="request in data.requests" :key="request.id"><td>{{ request.owner_id }}</td><td>{{ state[request.status] || request.status }}</td><td>¥{{ money(request.charged_micros) }}</td><td>{{ request.unknown_calls }}</td></tr></tbody>
      </table></div>
    </template>
  </section>
</template>
<style scoped>
section { margin: 18px 0; }
h3, h4 { margin: 16px 0 10px; }
p { margin: 8px 0; line-height: 1.6; }
small { display: block; color: #b5bed0; line-height: 1.6; }
.usage-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin: 12px 0; }
label { display: flex; align-items: center; gap: 8px; }
button, input { border: 1px solid #58617c; background: #2e344b; color: inherit; border-radius: 8px; padding: 8px 12px; font: inherit; }
input { width: 7rem; }
button { cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #424b65; white-space: nowrap; }
</style>
