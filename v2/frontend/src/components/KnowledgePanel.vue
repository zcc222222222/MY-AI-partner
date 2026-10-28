<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import * as api from '../api/knowledge'
import UsageAdmin from './UsageAdmin.vue'

const emit = defineEmits<{ close: [] }>()
const dialog = ref<HTMLDialogElement | null>(null)
const data = ref<api.KnowledgeOverview | null>(null)
const error = ref('')
const loading = ref(false)
const submitting = ref(false)
const recipe = ref('inspect')
const rechecking = ref(false)
const onlyFailures = ref(false)
const retryTopic = ref('逆境深塔')
const records = computed(() => (data.value?.retrievals ?? []).filter(r => !onlyFailures.value || r.reason !== 'success'))
const queryActive = computed(() => data.value?.retrievals?.some(r => r.state==='running') ?? false)
let disposed = false
let requestVersion = 0
let polling = false
let timer: ReturnType<typeof setInterval> | undefined
const active = computed(() => data.value?.jobs.some(j => ['queued', 'running'].includes(j.state)) ?? false)
const selected = computed(() => data.value?.recipes.find(r => r.id === recipe.value))
const labels: Record<string, string> = {
  queued: '等待启动', running: '运行中', completed: '流程完成', partial: '部分完成', failed: '失败', interrupted: '已中断',
  starting: '准备', collection: '采集', build: '编译与审核', roles: '角色同步', finished: '结束', stopped: '已停止',
  published: '已发布', unchanged: '无需更新', pending: '待处理', needs_attention: '待处理', retryable_failure: '暂时失败',
  unavailable: '暂无有效数据', deferred: '限额顺延', compiling: '编译中', reviewing: '审核中',
  current: '周期有效', recent: '近期检查成功', stale: '建议重新检查', not_checked: '未找到检查记录',
  ok: '上次访问成功', not_used: '本次服务启动后尚无联网查询', source_unavailable: '上次访问未成功', access_restricted: '站点访问受限，等待冷却',
  needs_review: '待复核', no_text_change: '所列结论无需改文', insufficient_evidence: '待补证', revision_required: '需修订', revision_published: '修订已发布',
}
const names: Record<string, string> = { mcguide: '官方整理的作者攻略', bwiki: 'BWIKI周期与日历', official: '鸣潮官方公告' }
const summaryNames: Record<string, string> = { new_sources: '新增来源', changed_sources: '变更来源', unchanged_sources: '未变来源',
  published_pages: '发布章节', unchanged_pages: '无需重编译', needs_attention: '待处理', retryable_pages: '暂时失败',
  deferred_pages: '限额顺延', http_requests: '网络请求', cache_hits: '缓存命中', completed: '已完成角色', pending: '待处理角色',
  retryable_failure: '暂时失败角色', unavailable: '缺少攻略', collection_failures: '未完成来源', sources: '来源', pages: '章节', dirty_pages: '失效章节', pending_pages: '待更新章节' }
const label = (value: string) => labels[value] ?? value
const diagnosticLabels: Record<string,string> = {success:'取得结果',no_match:'本次范围内无匹配',period_unverified:'当期周期未核实',
  scope_unverified:'适用范围未核实',cooldown:'冷却中',timeout:'请求超时',access_restricted:'网站限制访问',network_error:'网络连接失败',
  source_changed:'来源结构变化',robots_disallowed:'来源规则禁止采集',dependency_missing:'浏览器依赖不可用',source_unavailable:'来源暂不可用',
  invalid_request:'查询条件需确认',insufficient_evidence:'资料证据不足',internal_error:'工具执行异常',none:'未使用缓存',local:'本地资料',
  memory:'内存缓存',disk:'公开元数据缓存',network:'实时联网',cache:'读取缓存',robots:'检查采集规则',catalog:'查询目录',
  navigation:'网页加载',search_response:'搜索响应',sort:'结果排序',worker:'浏览器查询',launch:'启动浏览器',tool:'执行工具',validation:'校验参数',initializing:'准备浏览器'}
const diagnosticLabel = (value?: string) => value ? diagnosticLabels[value] || labels[value] || value : '—'
async function recheck(topic: string) {
  if (rechecking.value || queryActive.value) return
  rechecking.value = true
  try {
    const record = await api.recheck(topic)
    if (disposed || !data.value) return
    data.value.retrievals = [record,...data.value.retrievals.filter(r=>r.id!==record.id)].slice(0,20)
    error.value = ''
    await refresh()
  } catch (e) { if (!disposed) error.value = e instanceof Error ? e.message : '检查启动失败' }
  finally { if (!disposed) rechecking.value=false }
}
function date(value?: string) {
  if (!value) return '未记录'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '未记录' : new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }).format(parsed)
}
async function refresh() {
  const version = ++requestVersion
  loading.value = true
  try {
    const result = await api.overview()
    if (disposed || version !== requestVersion) return
    data.value = result
    error.value = ''
  } catch (e) { if (!disposed && version === requestVersion) error.value = e instanceof Error ? e.message : '读取失败' }
  finally { if (!disposed && version === requestVersion) loading.value = false }
}
async function submit(id?: string) {
  if (submitting.value || active.value) return
  submitting.value = true
  try {
    const result = id ? await api.resume(id) : await api.start(recipe.value)
    if (disposed) return
    if (data.value) data.value.jobs = [result, ...data.value.jobs.filter(j => j.id !== result.id)]
    error.value = ''
  } catch (e) { if (!disposed) error.value = e instanceof Error ? e.message : '任务启动失败' }
  finally { if (!disposed) submitting.value = false }
}
async function poll() {
  if (disposed || polling || !data.value) return
  const jobs = data.value.jobs.filter(j => ['queued', 'running'].includes(j.state))
  if (!jobs.length && !queryActive.value) return
  polling = true
  try {
    if (queryActive.value) {
      const result = await api.retrievals()
      if (disposed || !data.value) return
      data.value.retrievals = result.records
      if (!queryActive.value) await refresh()
    }
    for (const previous of jobs) {
      const result = await api.job(previous.id)
      if (disposed || !data.value) return
      data.value.jobs = data.value.jobs.map(j => j.id === result.id ? result : j)
      if (!['queued', 'running'].includes(result.state)) await refresh()
    }
  } catch (e) { if (!disposed) error.value = e instanceof Error ? e.message : '任务状态读取失败' }
  finally { polling = false }
}
onMounted(() => { dialog.value?.showModal(); void refresh(); timer = setInterval(() => void poll(), 6000) })
onBeforeUnmount(() => { disposed = true; requestVersion++; if (timer) clearInterval(timer) })
</script>

<template>
  <dialog ref="dialog" class="knowledge-panel" aria-labelledby="knowledge-title" @close="emit('close')">
    <header><div><h2 id="knowledge-title">知识库管理</h2><p>检查资料时效、执行更新及查看待处理事项</p></div>
      <button type="button" @click="emit('close')">返回聊天</button></header>
    <p v-if="error" role="alert" class="ops-error">{{ error }}</p>
    <button type="button" :disabled="loading" @click="refresh">{{ loading ? '正在读取…' : '刷新本地状态' }}</button>
    <template v-if="data">
      <UsageAdmin />
      <p class="ops-muted">状态检查：{{ date(data.checked_at) }}（北京时间）。刷新状态只读本地数据。</p>
      <div class="ops-stats">
        <div><strong>{{ data.counts.sources }}</strong><span>有效来源</span></div>
        <div><strong>{{ data.counts.pages }}</strong><span>章节记录（含历史）</span></div>
        <div><strong>{{ data.counts.pending_pages }}</strong><span>待更新章节</span></div>
        <div><strong>{{ data.counts.dirty_pages }}</strong><span>失效或已撤回章节</span></div>
      </div>
      <section><h3>来源检查</h3><div class="ops-cards">
        <article v-for="source in data.sources" :key="source.site">
          <h4>{{ names[source.site] }}</h4><p :class="{ 'ops-warning': source.freshness !== 'recent' }">{{ label(source.freshness) }}</p>
          <p>上次采集检查：{{ date(source.checked_at) }}</p><p>最近来源修订：{{ date(source.last_revision_at) }}</p>
          <small>检查成功超过 {{ source.check_after_days }} 天会提示更新；来源修订时间不代表最近检查时间。</small>
        </article>
      </div></section>
      <section><h3>玩法周期</h3><div class="ops-cards">
        <article v-for="period in data.periods" :key="period.topic"><h4>{{ period.topic }}</h4>
          <p :class="{ 'ops-warning': period.status !== 'current' }">{{ label(period.status) }}</p>
          <p v-if="period.start">{{ date(period.start) }} — {{ date(period.end) }}</p>
          <p v-else>需要采集并审核当期周期，系统会阻止往期图文冒充当期。</p>
        </article>
      </div></section>
      <section><h3>在线图文查询</h3>
        <p>{{ label(data.live_guides.status) }}</p>
        <p>最近尝试：{{ date(data.live_guides.last_attempt_at) }} · 最近成功：{{ date(data.live_guides.last_success_at) }}</p>
        <p v-if="data.live_guides.wait_seconds">下次可访问还需约 {{ data.live_guides.wait_seconds }} 秒。</p>
        <small>这里显示当前后端进程的访问状态，重启后重新记录；不记录玩家问题内容。</small>
      </section>
      <section><h3>最近检索记录</h3>
        <p class="ops-muted">展示最近20条工具调用；最多保留300条。只记录耗时、结果数量和失败类别，不保存玩家问题、账号身份、回答或原始异常。</p>
        <label><input v-model="onlyFailures" type="checkbox"> 仅看未成功取得结果的记录</label>
        <p v-if="!records.length" class="ops-muted">当前范围内没有记录；旧会话不会补写记录。</p>
        <article v-for="record in records" :key="record.id" class="ops-job">
          <h4>{{ record.topic || (record.tool === 'find_gameplay_guides' ? '在线攻略' : '本地资料检索') }} · {{ record.state === 'running' || record.state === 'interrupted' ? label(record.state) : diagnosticLabel(record.reason) }}</h4>
          <p>{{ date(record.started_at) }} · {{ record.origin === 'admin_recheck' ? '管理员检查' : record.origin === 'regression' ? '验收脚本' : '聊天工具调用' }} · {{ record.elapsed_ms == null ? '尚未结束' : (record.elapsed_ms / 1000).toFixed(2) + ' 秒' }}</p>
          <p>{{ diagnosticLabel(record.cache) }} · {{ diagnosticLabel(record.stage) }} · 返回 {{ record.result_count ?? 0 }} 项 · 图片引用 {{ record.image_count ?? 0 }} 张</p>
          <p v-if="record.reason === 'period_unverified'" class="ops-muted">先刷新并审核玩法周期；反复查询网站不能代替周期核实。</p>
          <button v-if="record.can_retry && record.topic" type="button" :disabled="rechecking || queryActive" @click="recheck(record.topic)">重新检查该玩法</button>
        </article>
        <p class="ops-muted">重新检查只查询所选玩法的当期攻略，不重放玩家原提问条件；不调用模型、不更新知识库。会联网刷新结果，保留网站冷却限制，管理员检查至少间隔60秒。</p>
        <label for="retry-topic">检查玩法</label>
        <select id="retry-topic" v-model="retryTopic" :disabled="rechecking || queryActive"><option>逆境深塔</option><option>冥歌海墟</option><option>终焉矩阵</option></select>
        <button type="button" :disabled="rechecking || queryActive" @click="recheck(retryTopic)">{{ queryActive ? '攻略查询运行中' : rechecking ? '正在启动…' : '重新检查所选玩法' }}</button>
      </section>
      <section><h3>执行更新</h3>
        <p v-if="active" class="ops-muted">任务在后台运行，关闭面板后仍会继续；完成后可回到此处查看结果。</p>
        <label for="ops-recipe">任务类型</label>
        <select id="ops-recipe" v-model="recipe" :disabled="active || submitting">
          <option v-for="item in data.recipes" :key="item.id" :value="item.id">{{ item.title }}</option>
        </select>
        <p>{{ selected?.description }}</p>
        <p v-if="selected?.live" class="ops-muted">每次只运行一个任务，采集至少间隔6秒。变化内容经过编译和审核，可能产生模型用量。</p>
        <button type="button" :disabled="active || submitting" @click="submit()">{{ active ? '已有任务运行中' : submitting ? '正在启动…' : selected?.live ? '执行更新' : '检查本地计划' }}</button>
      </section>
      <section><h3>公告复核</h3><p class="ops-muted">更新流程完成与攻略复核状态分别统计，待补证不会被自动标记为已验证。</p>
        <div class="ops-review"><span v-for="(count, state) in data.impact_reviews" :key="state">{{ label(String(state)) }}：{{ count }}</span></div>
      </section>
      <section v-if="data.pending_pages.length || data.planning_error_count"><h3>待更新事项</h3>
        <p v-if="data.planning_error_count" class="ops-warning">有 {{ data.planning_error_count }} 项本地规划错误。</p>
        <ul><li v-for="page in data.pending_pages" :key="page.page_id">{{ page.title }} · {{ label(page.status) }}</li></ul>
        <p v-if="data.counts.pending_pages > data.pending_pages.length" class="ops-muted">此处显示前 {{ data.pending_pages.length }} 项。</p>
      </section>
      <section><h3>任务记录</h3><p v-if="!data.jobs.length" class="ops-muted">尚未通过管理入口启动任务。</p>
        <article v-for="job in data.jobs" :key="job.id" class="ops-job">
          <h4>{{ data.recipes.find(r => r.id === job.recipe)?.title }} · {{ label(job.state) }}</h4>
          <p>第 {{ job.attempt }} 次执行 · {{ label(job.phase) }} · {{ date(job.updated_at) }}</p>
          <p v-if="job.progress?.name || job.progress?.site">{{ job.progress.name || names[job.progress.site || ''] }}</p>
          <p v-if="job.error" class="ops-warning">{{ job.error }}</p>
          <div v-if="job.report?.summary" class="ops-review"><span v-for="(count, key) in job.report.summary" :key="key">{{ summaryNames[String(key)] || key }}：{{ count }}</span></div>
          <ul v-if="job.report?.collection?.length"><li v-for="item in job.report.collection" :key="item.site">{{ names[item.site] }} · {{ label(item.status) }} · 检查导入 {{ item.imported }} 项</li></ul>
          <ul v-if="job.report?.issues?.length"><li v-for="(issue, i) in job.report.issues" :key="i">{{ issue.label }} · {{ label(issue.status) }}<span v-if="issue.retry_at"> · 冷却至 {{ date(issue.retry_at) }}</span></li></ul>
          <button v-if="job.can_resume" type="button" :disabled="active || submitting" @click="submit(job.id)">按原范围恢复任务</button>
        </article>
      </section>
    </template>
  </dialog>
</template>

<style scoped>
.knowledge-panel{width:min(1080px,94vw);max-height:90vh;overflow:auto;border:1px solid #52536c;border-radius:18px;padding:24px;background:#161827;color:#eef0f6;box-shadow:0 24px 90px #0009}
.knowledge-panel::backdrop{background:#050812b8;backdrop-filter:blur(8px)}
header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}h2,h3,h4{margin:0 0 10px}section{margin-top:26px}p{line-height:1.65;margin:8px 0}h3{font-size:1.05rem}.ops-muted,small{color:#b4b9cc}.ops-warning{color:#f2cb88}.ops-error{color:#ffadb5}
button,select{font:inherit;color:inherit;background:#30354d;border:1px solid #656c8b;border-radius:9px;padding:9px 14px}button{cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}select{margin:0 12px;max-width:100%}
.ops-stats,.ops-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(205px,1fr));gap:12px}.ops-stats>div,article{background:#202438;border:1px solid #3b425c;border-radius:12px;padding:16px}.ops-stats strong{font-size:1.7rem;display:block}.ops-stats span{color:#b4b9cc}.ops-review{display:flex;flex-wrap:wrap;gap:8px 18px}.ops-job{margin:12px 0}li{line-height:1.8;overflow-wrap:anywhere}@media(max-width:600px){.knowledge-panel{padding:16px}header{gap:8px}select{display:block;margin:10px 0;width:100%}}
</style>
