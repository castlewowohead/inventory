<script setup lang="ts">
/**
 * Extraction task status for one invoice (F05).
 *
 * The status comes from `GET /api/invoices/{id}/extraction`, which is persisted
 * on the backend: the user may leave while a task runs and the state is read
 * again on return. Polling stops as soon as every task reached a terminal state,
 * and on unmount - a view that is not on screen must not keep asking.
 *
 * There is **no percentage**: the backend reports `queued / running / succeeded /
 * failed` plus pages done, and inventing a progress bar out of that would be a
 * made-up number.
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import {
  errorMessage, getExtractionJobs, startExtraction,
  type ExtractionJob, type InvoiceFile
} from '../../shared/api'
import { formatClock } from '../../shared/format'

const props = defineProps<{
  invoiceId: number
  files: InvoiceFile[]
  isFake: boolean
  readOnly?: boolean
  hidden?: boolean
}>()

const emit = defineEmits<{
  /** A task reached a terminal state: the invoice (and its draft lines) changed. */
  finished: []
  /**
   * What this card currently has to say, so the page can keep it out of sight
   * when it has nothing to say. A finished task's own evidence is the values in
   * the result table; repeating "已完成 产出 3 条" above them costs a card and
   * tells the user nothing he cannot already see.
   */
  activity: [kind: 'loading' | 'busy' | 'failed' | 'warned' | 'done' | 'none' | 'error']
  jobs: [items: ExtractionJob[]]
}>()

/** Files the backend will create a task for (csv / xlsx import batches never run OCR). */
const RECOGNIZABLE = ['jpg', 'jpeg', 'png', 'pdf', 'heic']
const POLL_INTERVAL_MS = 2500
/** Stop asking after this long and hand the user a manual refresh instead. */
const POLL_LIMIT_MS = 6 * 60 * 1000

const jobs = ref<ExtractionJob[]>([])
const state = ref<'loading' | 'ready' | 'error'>('loading')
const failure = ref('')
const starting = ref(false)
const startError = ref('')
const pollingGaveUp = ref(false)
const showDiagnostics = ref(false)
let timer: ReturnType<typeof setTimeout> | null = null
let pollingSince = 0
let requestSequence = 0
let mounted = true

const statusLabel: Record<ExtractionJob['status'], string> = {
  queued: '排队中', running: '识别中', succeeded: '已完成', failed: '失败'
}
const recognizableFiles = computed(() => props.files.filter((file) => {
  const dot = file.original_name.lastIndexOf('.')
  return dot >= 0 && RECOGNIZABLE.includes(file.original_name.slice(dot + 1).toLowerCase())
}))
const busy = computed(() => jobs.value.some((job) => job.status === 'queued' || job.status === 'running'))
const canStart = computed(() =>
  !props.readOnly && !starting.value && jobs.value.length === 0 && recognizableFiles.value.length > 0)
const lineTotal = computed(() => jobs.value.reduce((sum, job) => sum + job.lines_created, 0))

/**
 * 需要老板动作的只有一类警告：**有行没被收进来**。实测里「模型报 5 行、
 * 行容量 3 行、丢弃 2 行」就是这一类，而那 2 行是真的采购行。
 * 其余 ⚠️ 是每张单据都一样的口径说明，留在诊断详情里。
 */
const latestJob = computed(() => jobs.value[0] ?? null)
/** One current result per file; older attempts remain available in diagnostics. */
const currentJobs = computed(() => {
  const fileIds = new Set(props.files.map((file) => file.id))
  const seen = new Set<number>()
  return jobs.value.filter((job) => {
    const id = job.invoice_file_id
    if (id === null || !fileIds.has(id) || seen.has(id)) return false
    seen.add(id)
    return true
  })
})
const failedJobs = computed(() => currentJobs.value.filter((job) => job.status === 'failed'))

/**
 * 这条警告说的是「**已经**少收了行」吗？
 *
 * 不能只匹配「截断」：后端还有一句解释截断规则的说明也含这个词（「比模型报出的
 * 行数少时会截断」），按词匹配会把一张一行没少的单据报成漏行。所以认完成态的
 * 措辞——「已按 N 行截断」与旧数据里的「已丢弃」。
 */
const droppedLines = (warning: string) =>
  (warning.includes('已按') && warning.includes('截断')) || warning.includes('已丢弃')

/**
 * 只用来判断「要不要报警」，原话不往界面上搬——那些句子是写给开发者的，
 * 完整内容在诊断详情里。
 */
const alerts = computed(() => (latestJob.value?.warnings ?? []).filter(droppedLines))
/** 历史任务不占版面；想看就点诊断详情。 */
const visibleJobs = computed(() => showDiagnostics.value
  ? jobs.value
  : latestJob.value
    ? [latestJob.value, ...failedJobs.value.filter((job) => job.id !== latestJob.value?.id)]
    : failedJobs.value)

/** 'done' is the only state the page may hide: everything else needs an action, a wait, or an eye. */
const activity = computed<'loading' | 'busy' | 'failed' | 'warned' | 'done' | 'none' | 'error'>(() => {
  if (state.value === 'loading') return 'loading'
  if (state.value === 'error') return 'error'
  if (busy.value) return 'busy'
  if (failedJobs.value.length) return 'failed'
  if (jobs.value.length === 0) return 'none'
  if (alerts.value.length) return 'warned'
  return 'done'
})
watch(activity, (kind) => emit('activity', kind), { immediate: true })

function fileName(job: ExtractionJob): string {
  const match = props.files.find((file) => file.id === job.invoice_file_id)
  return match ? `第 ${match.page_order} 页 · ${match.original_name}` : '原件已不在这张单据上'
}

function stopPolling() {
  if (timer !== null) {
    clearTimeout(timer)
    timer = null
  }
}

function schedulePoll() {
  stopPolling()
  // ⚠️ Never schedule after unmount: a request that was already in flight when
  // the user left would otherwise start the timer again, and a closed view would
  // keep asking the backend for the rest of the session.
  if (!mounted) return
  timer = setTimeout(() => { void load(true) }, POLL_INTERVAL_MS)
}

async function load(silent = false) {
  const sequence = (requestSequence += 1)
  if (!silent) {
    state.value = 'loading'
    failure.value = ''
  }
  try {
    const page = await getExtractionJobs(props.invoiceId)
    if (sequence !== requestSequence || !mounted) return
    const wasBusy = busy.value
    jobs.value = page.items
    emit('jobs', page.items)
    state.value = 'ready'
    failure.value = ''
    if (busy.value) {
      if (pollingSince === 0) pollingSince = Date.now()
      if (Date.now() - pollingSince < POLL_LIMIT_MS) {
        pollingGaveUp.value = false
        schedulePoll()
      } else {
        pollingGaveUp.value = true
        stopPolling()
      }
    } else {
      stopPolling()
      pollingSince = 0
      pollingGaveUp.value = false
      // Draft lines appear only when a task finishes, so tell the parent to re-read.
      if (wasBusy) emit('finished')
    }
  } catch (error) {
    if (sequence !== requestSequence || !mounted) return
    // A failed poll must not look like a finished task.
    stopPolling()
    state.value = 'error'
    failure.value = errorMessage(error)
  }
}

async function start() {
  if (starting.value) return
  starting.value = true
  startError.value = ''
  try {
    const page = await startExtraction(props.invoiceId)
    jobs.value = page.items
    emit('jobs', page.items)
    state.value = 'ready'
    pollingSince = Date.now()
    if (busy.value) schedulePoll()
    else emit('finished')
  } catch (error) {
    startError.value = errorMessage(error)
  } finally {
    starting.value = false
  }
}

watch(() => props.invoiceId, () => {
  stopPolling()
  pollingSince = 0
  jobs.value = []
  emit('jobs', [])
  startError.value = ''
  void load()
}, { immediate: true })

onUnmounted(() => {
  mounted = false
  stopPolling()
})
defineExpose({ refresh: () => load(false) })

</script>

<template>
  <view v-if="!hidden" class="ui-card section">
    <view class="section-heading">
      <text class="section-title">识别进度</text>
      <text v-if="isFake" class="demo-tag">演示识别结果</text>
    </view>

    <view v-if="state === 'loading'" class="muted" role="status" aria-live="polite">正在查看识别进度…</view>
    <template v-else-if="state === 'error'">
      <text class="field-error" role="alert">暂时无法查看识别进度：{{ failure }}</text>
      <button class="ui-button secondary" @click="load(false)">重试读取</button>
    </template>
    <template v-else>
      <text v-if="jobs.length === 0" class="muted">
        {{ readOnly ? '单据已完成，可回看原件与已入账行。' : recognizableFiles.length ? '可以开始识别，也可以对着原件手工录入。' : '请先上传可识别的单据文件。' }}
      </text>

      <view v-for="job in visibleJobs" :key="job.id" class="job" :class="job.status">
        <view class="job-head">
          <text class="job-status">{{ statusLabel[job.status] }}</text>
          <text v-if="job.provider === 'fake'" class="demo-tag">演示识别结果</text>
          <text class="muted">{{ fileName(job) }}</text>
        </view>
        <text v-if="job.status === 'succeeded'" class="muted">
          产出 {{ job.lines_created }} 条待核对行{{ job.pages_total ? ` · 处理 ${job.pages_done}/${job.pages_total} 页` : '' }}
        </text>
        <text v-else-if="job.status === 'failed'" class="field-error">
          识别没有完成。请重试；如果仍然失败，可以对着原件手工录入。
        </text>
        <text v-else class="muted">
          正在识别。可以先离开，稍后再回来查看。
        </text>
        <view v-if="job.id === latestJob?.id && alerts.length" class="alerts" role="status">
          <text class="alert-lead">识别没有把单据上的行全部收进来。请对着原件核对，缺的行用「加一行」补上。</text>
        </view>
        <view v-if="showDiagnostics" class="diagnostics">
          <text>第 {{ job.attempt }} 次识别</text>
          <text>开始：{{ formatClock(job.started_at) || '尚未开始' }} · 结束：{{ formatClock(job.finished_at) || '尚未结束' }}</text>
          <text v-if="job.warnings.length" class="warning">识别结果可能不完整，请对照原件核对每一行。</text>
        </view>
      </view>

      <text v-if="pollingGaveUp" class="history-note">
        已经查询了 6 分钟，任务还没结束。页面停止自动查询了，请点「刷新状态」再看一次。
      </text>
      <text v-if="startError" class="field-error" role="alert">{{ startError }}</text>

      <view class="actions">
        <button v-if="canStart" class="ui-button" @click="start">{{ starting ? '启动中…' : '开始识别' }}</button>
        <button v-if="jobs.length > 0" class="ui-button secondary" @click="load(true)">刷新状态</button>
        <button v-if="jobs.length > 0" class="ui-button ghost" :aria-pressed="showDiagnostics" @click="showDiagnostics = !showDiagnostics">
          {{ showDiagnostics ? '收起识别记录' : '识别记录' }}
        </button>
      </view>
    </template>
  </view>
</template>

<style scoped>
/* 识别器自己标的 ⚠️：和错误一样显眼，但它不是失败，是「这里请你看一眼」。 */
.alerts { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; border-left: 3px solid var(--warning); background: #fdf5ea; border-radius: 0 8px 8px 0; }
.alert-lead { color: var(--warning); font-size: 15px; font-weight: 700; line-height: 1.5; }
.section { display: flex; flex-direction: column; gap: 14px; padding: 22px; min-width: 0; }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.section-title { font-size: 18px; font-weight: 650; }
.demo-tag { flex: none; padding: 4px 8px; border-radius: 5px; background: #fff5e9; color: #704007; font-size: 12px; font-weight: 700; }
.job { display: flex; flex-direction: column; gap: 8px; padding: 14px; border: 1px solid var(--line); border-radius: 8px; }
.job.failed { border-color: var(--error); }
.job.succeeded { border-color: var(--green); }
.job-head { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.job-status { font-size: 15px; font-weight: 650; }
.diagnostics { display: flex; flex-direction: column; gap: 4px; padding-top: 8px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.diagnostics .warning { color: var(--muted); }
.field-error { color: var(--error); font-size: 13px; line-height: 1.5; }
.history-note { padding: 12px; border-radius: 6px; background: #fff5e9; color: #704007; font-size: 13px; line-height: 1.5; }
.hint { font-size: 13px; }
.actions { display: flex; flex-wrap: wrap; gap: 10px; }
@media (max-width: 899px) { .section { padding: 16px; } }
</style>
