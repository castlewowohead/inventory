<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  ApiError, createIntakeBatch, discardIntakeBatchFile, errorMessage, getCurrentIntakeBatch,
  getIntakeBatch, getIntakeBatchFileOriginal, processIntakeBatch,
  type IntakeBatch, type IntakeBatchFile
} from '../../shared/api'
import { formatBytes, formatLocalClock } from '../../shared/format'
import { downloadAttachment } from '../../shared/platform/download'
import { pickIntakeFiles, isCancelledPick, type PickedFile } from '../../shared/platform/filePicker'
import { registerLeaveGuard } from '../../shared/platform/leaveGuard'
import { uploadOne, type UploadHandle } from '../../shared/platform/upload'
import { DEFAULT_MAX_BYTES, extensionOf } from './uploadQueue'
import SpreadsheetImport from './SpreadsheetImport.vue'

const props = withDefaults(defineProps<{ batchId?: number | null; initialFiles?: PickedFile[] }>(), {
  batchId: null, initialFiles: () => []
})
const emit = defineEmits<{ opened: [id: number]; list: []; organize: [id: number] }>()
type LocalItem = { key: number; file: PickedFile; state: 'ready' | 'invalid' | 'uploading' | 'done' | 'failed'; progress: number; message: string }
const batch = ref<IntakeBatch | null>(null)
const queue = ref<LocalItem[]>([])
const loading = ref(true)
const running = ref(false)
const processing = ref(false)
const error = ref('')
const tableKey = ref<number | null>(null)
const askRemove = ref<IntakeBatchFile | null>(null)
const removing = ref(false)
const removeFailed = ref('')
const notice = ref('')
let nextKey = 0
let stopped = false
let active: UploadHandle<unknown> | null = null
let poll: ReturnType<typeof setInterval> | null = null
const tableItem = computed(() => queue.value.find((item) => item.key === tableKey.value) ?? null)
const pendingCount = computed(() => queue.value.filter((item) => item.state === 'ready' || item.state === 'failed').length)
const canUpload = computed(() => batch.value?.status === 'open')

function checkFile(file: PickedFile): string | null {
  const ext = extensionOf(file.name)
  if (!['jpg', 'jpeg', 'png', 'pdf', 'heic', 'csv', 'xlsx'].includes(ext)) return '仅支持 JPG、PNG、HEIC、PDF、CSV、XLSX。'
  if (file.size <= 0) return '文件为空，无法上传。'
  if (file.size > DEFAULT_MAX_BYTES) return `${formatBytes(file.size)} 超过默认单文件 20 MB 上限。`
  return null
}
function add(files: PickedFile[]) {
  for (const file of files) {
    const problem = checkFile(file)
    queue.value.push({ key: ++nextKey, file, state: problem ? 'invalid' : 'ready', progress: 0, message: problem ?? '' })
  }
}
function choose() {
  notice.value = ''
  // Keep the picker in the click gesture; a batch can contain more than one invoice.
  pickIntakeFiles().then((files) => { add(files); if (batch.value && !running.value && tableKey.value === null) void run() })
    .catch((reason) => { if (!isCancelledPick(reason)) error.value = errorMessage(reason) })
}
async function refresh() {
  if (!batch.value) return
  try { batch.value = await getIntakeBatch(batch.value.id); error.value = '' }
  catch (reason) { error.value = errorMessage(reason) }
}
async function process(retryFailed = false) {
  if (!batch.value || processing.value) return
  processing.value = true
  try { batch.value = (await processIntakeBatch(batch.value.id, retryFailed)).batch; error.value = '' }
  catch (reason) { error.value = errorMessage(reason) }
  finally { processing.value = false }
}
async function boot() {
  loading.value = true
  try {
    const opened = props.batchId ? await getIntakeBatch(props.batchId)
      : props.initialFiles.length ? await createIntakeBatch() : await getCurrentIntakeBatch()
    if (stopped) return
    batch.value = opened
    emit('opened', opened.id)
    if (opened.files.some((file) => file.status === 'pending')) void process()
    if (props.initialFiles.length) { add(props.initialFiles); void run() }
  } catch (reason) {
    if (!(reason instanceof ApiError && reason.status === 404 && props.batchId === null)) error.value = errorMessage(reason)
  } finally { loading.value = false }
}
async function create() {
  loading.value = true
  try { const opened = await createIntakeBatch(); batch.value = opened; emit('opened', opened.id); error.value = '' }
  catch (reason) { error.value = errorMessage(reason) }
  finally { loading.value = false }
}
async function run(retryFailed = false) {
  if (!canUpload.value || !batch.value || running.value || tableKey.value !== null) return
  running.value = true
  try {
    for (const item of queue.value) {
      if (stopped) break
      if (item.state !== 'ready' && !(retryFailed && item.state === 'failed')) continue
      if (['csv', 'xlsx'].includes(extensionOf(item.file.name))) { tableKey.value = item.key; break }
      item.state = 'uploading'; item.progress = 0; item.message = ''
      active = uploadOne(`/api/intake-batches/${batch.value.id}/files`, item.file, {
        fieldName: 'file', context: 'batch', onProgress: (value) => { item.progress = value.progress }
      })
      try {
        const result = await active.response as { file: { status: string; error: { message: string } | null } }
        item.state = result.file.status === 'failed' ? 'failed' : 'done'
        item.message = result.file.error?.message ?? ''
      } catch (reason) {
        if (reason instanceof ApiError && reason.status === 409 && typeof reason.details?.batch_file_id === 'number') {
          item.state = 'done'
          item.message = '这份文件已保存在当前批次。'
        } else {
          item.state = 'failed'
          item.message = errorMessage(reason)
        }
      }
      finally { active = null }
      if (!stopped) await refresh()
    }
    if (!stopped && tableKey.value === null) void process()
  } finally { running.value = false }
}
function tableStored() {
  if (tableItem.value) { tableItem.value.state = 'done'; tableItem.value.message = '' }
  tableKey.value = null
  void refresh().then(() => run())
}
function tableClosed() {
  if (tableItem.value) { tableItem.value.state = 'failed'; tableItem.value.message = '尚未完成表格映射，可继续上传后重试。' }
  tableKey.value = null
  void run()
}
function remove(key: number) { if (!running.value && tableKey.value === null) queue.value = queue.value.filter((item) => item.key !== key) }
/**
 * A file whose pages are already in a draft must stay: the draft is filled against
 * this original. The button says so before it is pressed instead of letting the
 * server answer with a 409 after the click (plan section 14).
 *
 * ⚠️ Since 2026-11-24 removing is a real delete (section 16): the pages and whatever
 * was classified on them go with the file, and re-uploading it starts from scratch.
 * That is why the button is red and the dialog says so in as many words.
 */
function blockedReason(file: IntakeBatchFile): string {
  if (!file.handed_off_item_count) return ''
  return `有 ${file.handed_off_item_count} 项已经开始填表或已入库，先在整理页撤回填表再移除。`
}
function startRemove(file: IntakeBatchFile) {
  if (!canUpload.value || blockedReason(file)) return
  removeFailed.value = ''
  askRemove.value = file
}
async function confirmRemove() {
  const file = askRemove.value
  if (!file || !batch.value || removing.value) return
  removing.value = true
  try {
    const result = await discardIntakeBatchFile(batch.value.id, file.id)
    // The local list is what the person just chose; keep it in step with the batch.
    queue.value = queue.value.filter((item) => !(item.state === 'done'
      && item.file.name === file.original_name && item.file.size === file.size))
    askRemove.value = null
    notice.value = result.removed_item_count
      ? `已移除 ${file.original_name}，连同它的 ${result.removed_item_count} 项内容和做过的分类。重新上传会当作全新的一份。`
      : `已移除 ${file.original_name}。重新上传会当作全新的一份。`
    await refresh()
  } catch (reason) {
    // Stays in the dialog: the person's finger is here, and the top of the card is not.
    removeFailed.value = errorMessage(reason)
    await refresh()
  } finally { removing.value = false }
}
async function original(fileId: number, name: string) {
  if (!batch.value) return
  try { downloadAttachment(await getIntakeBatchFileOriginal(batch.value.id, fileId), name) }
  catch (reason) { error.value = errorMessage(reason) }
}
const stopGuard = registerLeaveGuard(
  () => running.value || tableKey.value !== null ? '文件仍在上传或映射。离开会中断本地操作；已保存的文件仍在批次中。'
    : queue.value.some((item) => item.state === 'ready' || item.state === 'failed') ? '还有本地文件没有成功加入批次。离开后需要重新选择。' : null,
  () => { stopped = true; active?.abort(); queue.value = []; tableKey.value = null }
)
onMounted(() => { void boot(); poll = setInterval(() => { if (batch.value && !processing.value) void refresh() }, 2500) })
onUnmounted(() => { stopped = true; stopGuard(); active?.abort(); if (poll) clearInterval(poll) })
</script>

<template>
  <view class="ui-card batch-panel">
    <!-- 同第 19 节：批次号是库里的主键，界面上回答不了任何问题（这里也从来
         没有「挑一个批次」这回事）。它留在 URL、接口和下面这个属性里。 -->
    <text class="title" :data-batch-id="batch ? batch.id : undefined">上传单据</text>
    <text class="muted">图片与 PDF 按页、CSV 与 XLSX 按行保存。上传后仍需整理和审核，才会进入采购历史。</text>
    <button class="ui-button ghost back-list" @click="emit('list')">返回单据列表</button>
    <text v-if="loading" role="status">正在读取批次…</text>
    <view v-else-if="!batch" class="actions"><text>当前没有正在整理的批次。</text><button class="ui-button" @click="create">新建一批</button></view>
    <template v-else>
      <text v-if="!canUpload" class="muted">这个批次已结束，可回看文件；继续录入请新建一批。</text>
      <view class="actions">
        <!-- 主按钮是「开始填表」（2026-11-24 反馈，第 22 节）：文件传完了之后，这一页上
             要做的下一件事就是它；「继续选择文件」退成白色。原来那个名字「按日期和商家整理」
             说的是下一页叫什么，不是点它要干什么。 -->
        <button class="ui-button secondary" :disabled="!canUpload || running || tableKey !== null" @click="choose">继续选择文件</button>
        <button class="ui-button" :disabled="running || tableKey !== null" @click="emit('organize', batch.id)">开始填表</button>
        <button class="ui-button secondary" :disabled="processing" @click="refresh">刷新状态</button>
        <button v-if="batch.files.some(file => file.status === 'pending' || file.status === 'failed')" class="ui-button secondary" :disabled="processing" @click="process(true)">重试文件处理</button>
      </view>
      <text class="muted">可选 JPG、JPEG、PNG、HEIC、PDF、CSV、XLSX；单文件默认不超过 20 MB。服务端仍会最终校验。</text>
      <view v-if="batch.files.length" class="files saved">
        <text class="section-title">已保存的文件 · {{ batch.files.length }}</text>
        <text v-if="notice" class="muted" role="status">{{ notice }}</text>
        <view v-for="file in batch.files" :key="file.id" class="file-row">
          <view><text class="name">{{ file.original_name }}</text><text class="muted">{{ formatBytes(file.size) }} · {{ file.status === 'pending' ? '等待处理' : file.status === 'ready' ? `已处理 · ${file.item_count ?? 0} 项` : file.status === 'failed' ? '处理失败' : '已移除' }} · {{ formatLocalClock(file.created_at) }} 传入</text><text v-if="file.error" class="error">{{ file.error.message }}</text><text v-if="blockedReason(file)" class="muted">{{ blockedReason(file) }}</text></view>
          <view class="row-actions">
            <button class="ui-button ghost" @click="original(file.id, file.original_name)">下载原件</button>
            <!-- 传错了的文件在这里撤下来。暗着的那一个在上面一行说得出原因，手机上没有悬停也看得见。 -->
            <button v-if="canUpload" class="ui-button danger" :disabled="!!blockedReason(file)"
              :title="blockedReason(file) || undefined" :aria-label="`移除 ${file.original_name}`"
              @click="startRemove(file)">移除</button>
          </view>
        </view>
      </view>
      <view v-if="queue.length" class="files chosen">
        <text class="section-title">本次选择</text>
        <view v-for="item in queue" :key="item.key" class="file-row">
          <view><text class="name">{{ item.file.name }}</text><text class="muted">{{ formatBytes(item.file.size) }} · {{ item.state === 'invalid' ? '本地校验未通过' : item.state === 'uploading' ? '上传中' : item.state === 'done' ? '已保存' : item.state === 'failed' ? '待重试' : '待上传' }}</text><text v-if="item.message" class="error">{{ item.message }}</text><view v-if="item.state === 'uploading'" class="progress" role="progressbar" :aria-valuenow="item.progress" aria-valuemin="0" aria-valuemax="100"><view :style="{ width: item.progress + '%' }"></view></view><text v-if="item.state === 'uploading'" class="muted">{{ item.progress }}%</text></view>
          <button v-if="item.state !== 'done' && item.state !== 'uploading'" class="ui-button ghost" @click="remove(item.key)">移除</button>
        </view>
        <button v-if="pendingCount && tableKey === null" class="ui-button secondary" :disabled="!canUpload || running" @click="run(true)">继续上传待处理文件</button>
      </view>
      <SpreadsheetImport v-if="tableItem && batch" :key="tableItem.key" :batch-id="batch.id" :initial-file="tableItem.file" @stored="tableStored" @close="tableClosed" />
      <text v-if="processing" role="status">正在处理已保存的文件…</text>
    </template>
    <text v-if="error" role="alert" class="error">{{ error }}</text>

    <view v-if="askRemove" class="overlay" @click.self="askRemove = null">
      <view class="ui-card sheet" role="dialog" aria-modal="true" aria-label="移除这份文件">
        <text class="sheet-title">移除 {{ askRemove.original_name }}？</text>
        <text class="muted">
          它切出的 {{ askRemove.item_count ?? 0 }} 项内容会从整理页撤下，这些内容上做过的日期、商家分类一并删掉。
        </text>
        <text class="muted">
          这一步不能反悔：重新上传同一份文件会被当作全新的一份，之前做过的分类不会回来。
        </text>
        <text v-if="removeFailed" role="alert" class="error">{{ removeFailed }}</text>
        <view class="sheet-actions">
          <button class="ui-button danger" :disabled="removing" @click="confirmRemove">移除这份文件</button>
          <button class="ui-button secondary" :disabled="removing" @click="askRemove = null">取消</button>
        </view>
      </view>
    </view>
  </view>
</template>

<style scoped>
.batch-panel,.files { display:flex; flex-direction:column; gap:12px; padding:20px; }
.files { padding:0; }.title { font-size:21px; font-weight:700; }.section-title { font-size:16px; font-weight:650; }
.muted { color:var(--muted); font-size:13px; }.actions { display:flex; flex-wrap:wrap; align-items:center; gap:10px; }
.file-row { display:flex; justify-content:space-between; gap:12px; padding:12px; border:1px solid var(--line); border-radius:8px; }
.file-row > view:first-child { min-width:0; display:flex; flex-direction:column; gap:4px; }.name { overflow-wrap:anywhere; }
.error { color:var(--error); font-size:13px; }.progress { width:100%; height:8px; background:#e6ece7; border-radius:4px; overflow:hidden; }.progress view { height:100%; background:var(--green); }
.back-list { align-self:flex-start; }
.row-actions { display:flex; flex-wrap:wrap; align-items:center; gap:8px; }
/* 红的：它真的会把这份文件的内容和分类删掉（第 16 节）。确认仍在对话框里问一次。 */
.ui-button.danger { border-color:var(--error); background:var(--error); color:#fff; }
.overlay { position:fixed; inset:0; z-index:20; display:flex; align-items:center; justify-content:center; padding:16px; background:var(--dialog-scrim); }
.sheet { width:min(100%, 420px); max-height:86vh; overflow-y:auto; display:flex; flex-direction:column; gap:10px; padding:20px; }
.sheet-title { font-size:16px; font-weight:700; overflow-wrap:anywhere; }
.sheet-actions { display:flex; flex-wrap:wrap; gap:10px; margin-top:4px; }
@media(max-width:899px) { .batch-panel { padding:16px; }.file-row { flex-direction:column; } }
</style>
