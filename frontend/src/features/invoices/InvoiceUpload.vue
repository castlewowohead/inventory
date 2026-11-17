<script setup lang="ts">
/**
 * File selection and upload for one invoice (F05).
 *
 * `mode="create"` builds a new invoice from the selected pages; `mode="append"`
 * adds pages to an invoice that already exists (a page that failed half way, or
 * one that was missed). The request order and the reason for it live in
 * `uploadQueue.ts`; this component only shows what is happening.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { formatBytes } from '../../shared/format'
import {
  PickerUnavailableError, isCancelledPick, pickDocuments, pickPhotos,
  type PickedFile
} from '../../shared/platform/filePicker'
import { registerLeaveGuard } from '../../shared/platform/leaveGuard'
import {
  ALLOWED_EXTENSIONS, MAX_FILES_PER_INVOICE, useUploadQueue, type ItemState, type RunOutcome
} from './uploadQueue'

const props = withDefaults(defineProps<{
  mode: 'create' | 'append'
  invoiceId?: number | null
  existingFiles?: number
  initialFiles?: PickedFile[]
  autoStart?: boolean
}>(), { invoiceId: null, existingFiles: 0, initialFiles: () => [], autoStart: false })

const emit = defineEmits<{
  /** A new invoice is ready to open (create mode). */
  uploaded: [invoiceId: number]
  /** Files were added to the invoice being viewed (append mode). */
  stored: []
  openExisting: [invoiceId: number]
  close: []
}>()

const {
  items, running, invoiceId, hasInvalid, hasHeic, doneCount, remainingCount,
  add, remove, move, run, cancel, reset
} = useUploadQueue()

const manualOnly = ref(false)
const notice = ref('')
const outcome = ref<RunOutcome | null>(null)

const slotsLeft = computed(() =>
  Math.max(0, MAX_FILES_PER_INVOICE - props.existingFiles - items.value.length))
const canStart = computed(() => !running.value && remainingCount.value > 0 && !hasInvalid.value)
const startLabel = computed(() => {
  if (running.value) return '上传中…'
  const count = remainingCount.value
  if (doneCount.value > 0) return `继续上传剩下 ${count} 个`
  return props.mode === 'create'
    ? (manualOnly.value ? `只保存 ${count} 个文件` : `上传 ${count} 个文件并识别`)
    : `上传 ${count} 个文件`
})
const stateLabel: Record<ItemState, string> = {
  ready: '待上传', invalid: '不能上传', uploading: '上传中', done: '已保存', failed: '上传失败'
}

const stopGuard = registerLeaveGuard(
  () => {
    if (running.value) return '文件正在上传。离开会中断上传，已保存的页仍留在单据上。'
    if (doneCount.value > 0 && remainingCount.value > 0) {
      return `还有 ${remainingCount.value} 个文件没有上传完。离开后可以在单据详情里补传。`
    }
    if (remainingCount.value > 0) return '已选择的文件还没有上传。离开会丢掉这次选择。'
    return null
  },
  () => {
    // The user chose to leave: stop sending bytes and drop the selection, so a
    // panel that stays mounted does not ask about it again.
    cancel()
    reset()
    outcome.value = null
  }
)
onUnmounted(() => stopGuard())

/** uni's `switch` reports the new value in `event.detail.value`; the bundled type omits it. */
function toggleManualOnly(event: unknown) {
  const detail = (event as { detail?: { value?: unknown } }).detail
  manualOnly.value = Boolean(detail?.value)
}

function take(files: PickedFile[]) {
  outcome.value = null
  notice.value = add(files, props.existingFiles) ?? ''
}

function reportPickFailure(reason: unknown) {
  if (isCancelledPick(reason)) return
  notice.value = reason instanceof PickerUnavailableError
    ? reason.message
    : '打开文件选择失败，请重试或换一个入口。'
}

/** Pickers run straight from the click handler: H5 needs the user gesture. */
/**
 * 现拍一张。手机浏览器会直接开相机。
 *
 * 「从相册选」没有单独的按钮：文件选择器本来就收图片，而手机浏览器的文件选择
 * 第一项就是照片图库。两个按钮都写着「图片」只会让人以为它们挑的不是一回事。
 */
function choosePhotos(from: 'camera' | 'album') {
  pickPhotos(from, Math.max(1, slotsLeft.value)).then(take).catch(reportPickFailure)
}

function chooseDocuments() {
  pickDocuments(Math.max(1, slotsLeft.value)).then(take).catch(reportPickFailure)
}

async function start() {
  notice.value = ''
  const result = await run({
    invoiceId: props.mode === 'append' ? props.invoiceId ?? null : invoiceId.value,
    startExtractionWhenDone: props.mode === 'create' && !manualOnly.value
  })
  outcome.value = result
  if (props.mode === 'append' && result.uploaded > 0) emit('stored')
  if (result.failed || result.invoiceId === null) return
  if (props.mode === 'create') emit('uploaded', result.invoiceId)
  reset()
}

function close() {
  if (running.value) return
  reset()
  outcome.value = null
  notice.value = ''
  emit('close')
}

function openPartial() {
  const id = outcome.value?.invoiceId
  if (!id) return
  // Asking "you have unsaved files" right after the user asked to open the
  // invoice would be noise: the remaining pages are uploaded there instead.
  reset()
  outcome.value = null
  emit('uploaded', id)
}

function openExisting() {
  const id = outcome.value?.existingInvoiceId
  if (!id) return
  reset()
  outcome.value = null
  emit('openExisting', id)
}

onMounted(() => {
  if (!props.initialFiles.length) return
  take(props.initialFiles)
  if (props.autoStart && canStart.value) void start()
})
</script>

<template>
  <view class="ui-card upload-panel" :class="props.mode">
    <view class="panel-head">
      <text class="section-title">{{ props.mode === 'create' ? '上传单据' : '补传文件' }}</text>
      <button v-if="props.mode === 'create'" class="ui-button ghost" :disabled="running" @click="close">关闭</button>
    </view>

    <view class="entries">
      <button class="ui-button secondary" :disabled="running || slotsLeft === 0" @click="choosePhotos('camera')">拍照</button>
      <button class="ui-button secondary" :disabled="running || slotsLeft === 0" @click="chooseDocuments">选择图片或 PDF</button>
    </view>
    <text class="muted hint">
      可传 {{ ALLOWED_EXTENSIONS.join(' / ') }}；单个文件默认上限 20 MB，一张单据最多 {{ MAX_FILES_PER_INVOICE }} 个文件（还可加 {{ slotsLeft }} 个）。实际上限由后端配置决定。
    </text>
    <text class="muted hint">CSV / XLSX 请从单据列表的「导入表格 / 导出明细」进入。</text>

    <view v-if="props.mode === 'create'" class="manual-toggle">
      <switch :checked="manualOnly" :disabled="running" color="#176b4d" @change="toggleManualOnly" />
      <text>只保存文件，手工录入（不启动识别）</text>
    </view>

    <text v-if="notice" class="field-error" role="alert">{{ notice }}</text>
    <text v-if="hasHeic" class="history-note">
      HEIC 原件可以保存；能否在浏览器里预览、能否识别取决于后端与浏览器当前的支持情况，页面不替它们承诺。
    </text>

    <view v-if="items.length" class="queue">
      <view v-for="(item, index) in items" :key="item.key" class="queue-row" :class="item.state">
        <text class="order">第 {{ props.existingFiles + index + 1 }} 页</text>
        <view class="file-main">
          <text class="file-name">{{ item.file.name }}</text>
          <text class="muted">{{ formatBytes(item.file.size) }} · {{ stateLabel[item.state] }}</text>
          <view v-if="item.state === 'uploading'" class="progress" role="progressbar" :aria-valuenow="item.progress" aria-valuemin="0" aria-valuemax="100">
            <view class="progress-fill" :style="{ width: item.progress + '%' }"></view>
          </view>
          <text v-if="item.state === 'uploading'" class="muted">{{ item.progress }}%</text>
          <text v-if="item.message" class="field-error">{{ item.message }}</text>
        </view>
        <view class="row-actions">
          <button class="ui-button ghost" :disabled="running || index === 0 || item.state === 'done'" @click="move(item.key, -1)">上移</button>
          <button class="ui-button ghost" :disabled="running || index === items.length - 1 || item.state === 'done'" @click="move(item.key, 1)">下移</button>
          <button class="ui-button ghost" :disabled="running || item.state === 'done'" @click="remove(item.key)">移除</button>
        </view>
      </view>
      <text class="muted hint">上传顺序就是单据的页序；上传前可以调整。</text>
    </view>

    <view v-if="outcome" class="outcome" role="status" aria-live="polite">
      <template v-if="outcome.failed">
        <text class="field-error">{{ outcome.message }}</text>
        <text v-if="outcome.uploaded > 0 && outcome.invoiceId" class="history-note">
          单据 #{{ outcome.invoiceId }} 已经建立，前 {{ outcome.uploaded }} 页已保存，识别没有启动。可以在这里重试剩下的页，或打开这张单据稍后补传；补齐后由你启动识别。
        </text>
        <view class="outcome-actions">
          <button v-if="canStart" class="ui-button" @click="start">重试剩下的页</button>
          <button v-if="outcome.existingInvoiceId" class="ui-button secondary" @click="openExisting">打开已有单据 #{{ outcome.existingInvoiceId }}</button>
          <button v-if="outcome.invoiceId && props.mode === 'create'" class="ui-button secondary" @click="openPartial">打开这张单据</button>
        </view>
      </template>
      <template v-else>
        <text v-if="outcome.extraction === 'failed'" class="field-error">{{ outcome.message }}</text>
        <text v-else class="muted">{{ outcome.uploaded }} 个文件已保存{{ outcome.extraction === 'started' ? '，识别已开始' : '' }}。</text>
      </template>
    </view>

    <view class="panel-actions">
      <button class="ui-button" :disabled="!canStart" @click="start">{{ startLabel }}</button>
      <button v-if="running" class="ui-button secondary" @click="cancel">中断上传</button>
      <button v-else-if="props.mode === 'append' && items.length" class="ui-button secondary" @click="reset">清空选择</button>
    </view>
  </view>
</template>

<style scoped>
.upload-panel { display: flex; flex-direction: column; gap: 14px; padding: 22px; }
.upload-panel.append { border-style: dashed; }
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.section-title { font-size: 18px; font-weight: 650; }
.entries { display: flex; flex-wrap: wrap; gap: 10px; }
.hint { font-size: 13px; line-height: 1.5; }
.manual-toggle { display: flex; align-items: center; gap: 10px; min-height: 44px; font-size: 14px; }
.field-error { color: var(--error); font-size: 13px; line-height: 1.5; }
.history-note { padding: 12px; border-radius: 6px; background: #fff5e9; color: #704007; font-size: 13px; line-height: 1.5; }
.queue { display: flex; flex-direction: column; gap: 10px; }
.queue-row { display: flex; align-items: flex-start; gap: 12px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; }
.queue-row.failed, .queue-row.invalid { border-color: var(--error); }
.queue-row.done { border-color: var(--green); }
.order { flex: none; color: var(--muted); font-size: 13px; }
.file-main { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 6px; }
.file-name { overflow-wrap: anywhere; font-size: 15px; }
.progress { height: 8px; width: 100%; border-radius: 4px; background: #e6ece7; overflow: hidden; }
.progress-fill { height: 8px; background: var(--green); }
.row-actions { flex: none; display: flex; gap: 2px; }
.row-actions .ui-button { padding: 8px 6px; font-size: 13px; }
.outcome { display: flex; flex-direction: column; gap: 10px; }
.outcome-actions, .panel-actions { display: flex; flex-wrap: wrap; gap: 10px; }
@media (max-width: 899px) {
  .upload-panel { padding: 16px; }
  .entries .ui-button { flex: 1 1 auto; }
  .queue-row { flex-direction: column; }
  .row-actions { width: 100%; justify-content: flex-start; }
}
</style>
