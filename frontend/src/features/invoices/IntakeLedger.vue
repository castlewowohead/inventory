<script setup lang="ts">
/** A paged, continuous view of the same intake items and M06 moves as the card board. */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  ApiError, errorMessage, getCurrentIntakeBatch, getIntakeBatch,
  getIntakeItems, handOffIntakeItems, moveIntakeItems, undoIntakeMove,
  type IntakeBatch, type IntakeItem, type IntakeSource,
  type IntakeItemFilters
} from '../../shared/api'
import { isNarrowViewport, watchViewport } from '../../shared/platform/viewport'
import OriginalDocumentViewer from './OriginalDocumentViewer.vue'
import {
  cardLocation, formState, isHandedOff, isIsoDate, missingLabel, recognitionNote, suggestionsOf
} from './intakeBoard'

const props = withDefaults(defineProps<{ batchId?: number | null; active?: boolean }>(), { batchId: null, active: true })
const emit = defineEmits<{ cards: []; upload: []; opened: [id: number]; review: [id: number] }>()
const PAGE_SIZE = 50
const batch = ref<IntakeBatch | null>(null)
const rows = ref<IntakeItem[]>([])
const total = ref(0)
const offset = ref(0)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const selected = ref<IntakeItem[]>([])
const preview = ref<IntakeItem | null>(null)
const lastMove = ref<number | null>(null)
const retry = ref<{ key: string; action: 'date' | 'source' | 'group' | 'clear'; items: IntakeItem[]; date: string; source: string } | null>(null)
const filterDate = ref('')
const filterSource = ref('')
const missing = ref<IntakeItemFilters['missing'] | ''>('')
const editDate = ref('')
const editSource = ref('')
const narrow = ref(isNarrowViewport())
/** 标题旁边那个「▾」展开的显示形式菜单（卡片整理 / 财务表格）。 */
const viewMenu = ref(false)
let stopViewport = () => {}
let requestSerial = 0
let loadSerial = 0
const selectedIds = computed(() => new Set(selected.value.map((item) => item.id)))
const allPageSelected = computed(() => rows.value.length > 0 && rows.value.every((item) => selectedIds.value.has(item.id)))
const pageNumber = computed(() => Math.floor(offset.value / PAGE_SIZE) + 1)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const canMove = computed(() => batch.value?.status === 'open')
const previewSource = computed<IntakeSource[]>(() => preview.value ? [{
  batch_id: preview.value.batch_id, item_id: preview.value.id,
  batch_file_id: preview.value.batch_file_id, version: preview.value.version,
  kind: preview.value.kind, page_no: preview.value.page_no, row_no: preview.value.row_no,
  original_name: preview.value.original_name, content_type: null
}] : [])

function filters(): IntakeItemFilters {
  return {
    ...(filterDate.value ? { purchaseDate: filterDate.value } : {}),
    ...(filterSource.value.trim() ? { sourceName: filterSource.value.trim() } : {}),
    ...(missing.value ? { missing: missing.value } : {})
  }
}

async function loadRows() {
  if (!batch.value) return
  const serial = ++loadSerial
  loading.value = true
  try {
    const page = await getIntakeItems(batch.value.id, PAGE_SIZE, offset.value, filters())
    if (serial !== loadSerial) return
    if (offset.value > 0 && offset.value >= page.total) {
      offset.value = page.total ? Math.floor((page.total - 1) / PAGE_SIZE) * PAGE_SIZE : 0
      await loadRows()
      return
    }
    rows.value = page.items
    total.value = page.total
    const fresh = new Map(page.items.map((item) => [item.id, item]))
    selected.value = selected.value.map((item) => fresh.get(item.id) ?? item)
    error.value = ''
  } catch (reason) {
    if (serial === loadSerial) error.value = errorMessage(reason)
  } finally {
    if (serial === loadSerial) loading.value = false
  }
}

async function boot() {
  try {
    const opened = props.batchId ? await getIntakeBatch(props.batchId) : await getCurrentIntakeBatch()
    batch.value = opened
    emit('opened', opened.id)
    await loadRows()
  } catch (reason) { error.value = errorMessage(reason) }
}

function applyFilters() {
  if (filterDate.value && filterDate.value !== 'missing' && !isIsoDate(filterDate.value)) {
    error.value = '筛选日期请填合法的 YYYY-MM-DD。'
    return
  }
  offset.value = 0
  selected.value = []
  void loadRows()
}

function resetFilters() {
  filterDate.value = ''
  filterSource.value = ''
  missing.value = ''
  applyFilters()
}

function flip(item: IntakeItem) {
  selected.value = selectedIds.value.has(item.id)
    ? selected.value.filter((row) => row.id !== item.id) : [...selected.value, item]
}

function flipPage() {
  if (allPageSelected.value) {
    const ids = new Set(rows.value.map((item) => item.id))
    selected.value = selected.value.filter((item) => !ids.has(item.id))
  } else {
    selected.value = [...selected.value, ...rows.value.filter((item) => !selectedIds.value.has(item.id))]
  }
}

function goPage(next: number) {
  if (next < 1 || next > pageCount.value) return
  offset.value = (next - 1) * PAGE_SIZE
  void loadRows()
}

async function submitMove(action: 'date' | 'source' | 'group' | 'clear', items = selected.value,
  key?: string, date = editDate.value.trim(), source = editSource.value.trim()) {
  if (!batch.value || !items.length || busy.value) return
  if (!canMove.value) { error.value = '这个批次已关闭，不能修改分类。'; return }
  if (items.every(isHandedOff)) {
    error.value = '选中的行都已经进入单据审核，这里不能再改分类；请在审核里修改分类。'
    return
  }
  items = items.filter((item) => !isHandedOff(item))
  if ((action === 'date' || action === 'group') && !isIsoDate(date)) {
    error.value = '请填写合法的采购日期（YYYY-MM-DD）。'; return
  }
  if ((action === 'source' || action === 'group') && !source) {
    error.value = '请填写商家名称。'; return
  }
  busy.value = true
  error.value = ''
  const requestKey = key ?? `ledger-${batch.value.id}-${Date.now().toString(36)}-${++requestSerial}`
  try {
    const result = await moveIntakeItems(batch.value.id, {
      request_key: requestKey, action,
      items: items.map((item) => ({ id: item.id, version: item.version })),
      ...(action === 'date' || action === 'group' ? { purchase_date: date } : {}),
      ...(action === 'source' || action === 'group' ? { source_name: source } : {})
    })
    const ids = new Set(result.items.map((item) => item.id))
    selected.value = selected.value.filter((item) => !ids.has(item.id))
    lastMove.value = result.move_id
    retry.value = null
    notice.value = `已更新 ${result.items.length} 行；切回卡片页可看同一结果。`
    await loadRows()
  } catch (reason) {
    if (reason instanceof ApiError && reason.status === 409) {
      error.value = `${reason.message}（本次没有行被改动）`
      retry.value = null
      await loadRows()
    } else {
      error.value = errorMessage(reason)
      retry.value = { key: requestKey, action, items, date, source }
    }
  } finally { busy.value = false }
}

function retryMove() {
  const attempt = retry.value
  if (attempt) void submitMove(attempt.action, attempt.items, attempt.key, attempt.date, attempt.source)
}

async function undo() {
  if (!batch.value || !lastMove.value || busy.value) return
  busy.value = true
  try {
    await undoIntakeMove(batch.value.id, lastMove.value)
    lastMove.value = null
    notice.value = '已撤销上一步分类。'
    await loadRows()
  } catch (reason) { error.value = errorMessage(reason) }
  finally { busy.value = false }
}

async function openReview(item: IntakeItem) {
  if (!batch.value || busy.value) return
  // Already 编辑中: go back to that draft instead of asking the server again.
  if (item.review_invoice_id !== null) { emit('review', item.review_invoice_id); return }
  if (!item.decided_purchase_date || !item.decided_source_name) {
    error.value = '先确认这一行的采购日期和商家，才能进入明细审核。'
    return
  }
  busy.value = true
  try {
    const result = await handOffIntakeItems(batch.value.id, [{ id: item.id, version: item.version }])
    emit('review', result.invoice_id)
  } catch (reason) {
    error.value = errorMessage(reason)
    if (reason instanceof ApiError && reason.status === 409) await loadRows()
  } finally { busy.value = false }
}

function cell(item: IntakeItem, name: string): string {
  const value = item.row_values?.[name]
  return typeof value === 'string' ? value : '—'
}

function candidate(item: IntakeItem, field: 'purchase_date' | 'source_name'): string {
  return suggestionsOf(item).filter((chip) => chip.field === field)
    .map((chip) => `${chip.text}（${chip.basis}）`).join('；')
}

watch(() => props.batchId, (next, previous) => {
  if (next !== previous && props.active) { selected.value = []; offset.value = 0; void boot() }
})
watch(() => props.active, (active) => {
  if (active) { if (!batch.value) void boot(); else void loadRows() }
})
onMounted(() => {
  stopViewport = watchViewport((value) => { narrow.value = value })
  if (props.active) void boot()
})
onUnmounted(() => { stopViewport(); loadSerial++ })
</script>

<template>
  <view class="ledger ui-card">
    <view class="heading">
      <!-- 回卡片整理的入口和进来时那个「▾」在同一个位置：左上角、标题旁边
           （第 18 节 ③）。两种显示形式用同一个控件换，来回是一条路。 -->
      <view>
        <text class="eyebrow">同一批次 · 财务表格</text>
        <view class="title-row">
          <text class="sheet-title">按行核对采购日期与商家</text>
          <view class="view-switch">
            <button class="caret" :aria-expanded="viewMenu ? 'true' : 'false'" aria-label="切换显示形式"
              @click="viewMenu = !viewMenu">▾</button>
            <view v-if="viewMenu" class="view-menu">
              <button class="ui-button ghost" @click="viewMenu = false; emit('cards')">卡片整理形式</button>
            </view>
          </view>
        </view>
      </view>
      <view class="heading-actions"><button class="ui-button secondary" @click="emit('upload')">查看上传文件</button></view>
    </view>
    <view v-if="narrow" class="mobile-note">窄屏请使用卡片整理页；表格保留给桌面核对。</view>
    <template v-else>
      <view class="filters">
        <label>采购日期 <input v-model="filterDate" placeholder="YYYY-MM-DD" @keyup.enter="applyFilters" /></label>
        <label>商家 <input v-model="filterSource" placeholder="商家名称" @keyup.enter="applyFilters" /></label>
        <label>待定原因 <select v-model="missing"><option value="">全部</option><option value="any">缺日期或商家</option><option value="both">日期与商家都缺</option><option value="date_any">日期缺失（含商家缺失）</option><option value="source_any">商家缺失（含日期缺失）</option><option value="date">仅缺日期</option><option value="source">仅缺商家</option><option value="recognition_failed">识别失败</option></select></label>
        <button class="ui-button" @click="applyFilters">筛选</button><button class="ui-button secondary" @click="resetFilters">清除</button>
      </view>
      <view v-if="error" class="message error" role="alert">{{ error }} <button v-if="retry" @click="retryMove">重试这一步</button></view>
      <view v-if="notice" class="message" role="status">{{ notice }} <button v-if="lastMove" @click="undo">撤销这一步</button></view>
      <view class="bulk">
        <text>已选 {{ selected.length }} 行</text>
        <input v-model="editDate" placeholder="采购日期 YYYY-MM-DD" aria-label="批量修改采购日期" />
        <input v-model="editSource" placeholder="商家名称" aria-label="批量修改商家" />
        <button :disabled="!selected.length || busy" @click="submitMove('date')">只改日期</button>
        <button :disabled="!selected.length || busy" @click="submitMove('source')">只改商家</button>
        <button :disabled="!selected.length || busy" @click="submitMove('group')">同时改两项</button>
        <button :disabled="!selected.length || busy" @click="submitMove('clear')">清空决定</button>
      </view>
      <view class="table-scroll">
        <table><thead><tr><th><checkbox :checked="allPageSelected" aria-label="选择本页" @click="flipPage" /></th><th>原件 / 坐标</th><th>商品描述</th><th>数量</th><th>单价</th><th>采购日期</th><th>商家</th><th>待定原因</th><th>操作</th></tr></thead>
          <tbody><tr v-for="item in rows" :key="item.id">
            <td><checkbox :checked="selectedIds.has(item.id)" :aria-label="`选择 ${item.original_name} ${cardLocation(item)}`" @click="flip(item)" /></td>
            <td><strong>{{ item.original_name }}</strong><small>{{ cardLocation(item) }}</small></td>
            <td>{{ cell(item, 'original_description') }}</td><td class="numeric">{{ cell(item, 'quantity') }}</td><td class="numeric">{{ cell(item, 'unit_price') }}</td>
            <td><strong v-if="item.decided_purchase_date">{{ item.decided_purchase_date }}</strong><span v-else class="pending">待确认</span><small v-if="candidate(item, 'purchase_date')">建议：{{ candidate(item, 'purchase_date') }}</small></td>
            <td><strong v-if="item.decided_source_name">{{ item.decided_source_name }}</strong><span v-else class="pending">待确认</span><small v-if="candidate(item, 'source_name')">建议：{{ candidate(item, 'source_name') }}</small></td>
            <td>{{ formState(item)?.label ?? (missingLabel(item) || (item.recognition.status === 'failed' ? '识别失败' : '已分类')) }}<small v-if="item.recognition.status === 'failed'">{{ recognitionNote(item) }}</small><small v-else-if="isHandedOff(item)">分类改动请在填表页做，或在卡片页撤回填表</small></td>
            <td><button @click="preview = item">看原件</button><button :disabled="busy" @click="openReview(item)">{{ formState(item)?.action === 'refill' ? '查看单据' : isHandedOff(item) ? '继续填表' : '开始填表' }}</button></td>
          </tr><tr v-if="!loading && !rows.length"><td colspan="9">没有符合条件的内容。</td></tr></tbody>
        </table>
      </view>
      <view class="pager"><text>{{ loading ? '正在读取…' : `共 ${total} 行 · 每页最多 ${PAGE_SIZE} 行 · 第 ${pageNumber} / ${pageCount} 页` }}</text><button :disabled="offset === 0 || loading" @click="goPage(pageNumber - 1)">上一页</button><button :disabled="pageNumber >= pageCount || loading" @click="goPage(pageNumber + 1)">下一页</button></view>
    </template>
    <view v-if="preview" class="overlay" @click.self="preview = null"><view class="preview-card ui-card"><view class="preview-head"><text>{{ preview.original_name }} · {{ cardLocation(preview) }}</text><button @click="preview = null">关闭</button></view><OriginalDocumentViewer class="viewer" :files="[]" :sources="previewSource" eyebrow="批次原件" :is-fake="preview.recognition.is_fake === true" :focus="null" :visible="true" /></view></view>
  </view>
</template>

<style scoped>
.ledger { padding: 20px; min-width: 0; }
.heading, .heading-actions, .filters, .bulk, .pager, .preview-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.heading { justify-content: space-between; margin-bottom: 16px; }
.heading > :first-child { display: grid; gap: 4px; }
.title-row { display: flex; align-items: center; gap: 6px; }
/* 和整理页标题旁那个「▾」一模一样：同一个控件，换一种显示形式。 */
.view-switch { position: relative; display: flex; align-items: center; }
.caret { width: 36px; min-height: 36px; margin: 0; padding: 0; border: 0; background: transparent; color: var(--green); font-size: 16px; line-height: 1; cursor: pointer; }
.caret::after { border: 0; }
.view-menu { position: absolute; top: calc(100% + 4px); left: 0; z-index: 8; min-width: 160px; padding: 6px; border: 1px solid var(--line); border-radius: 8px; background: #fff; box-shadow: 0 10px 28px #173b2e20; }
.view-menu .ui-button { width: 100%; margin: 0; text-align: left; }
.filters, .bulk { padding: 12px 0; border-top: 1px solid var(--line); }
.filters :deep(uni-label) { display: grid; gap: 4px; font-size: 13px; }
:deep(uni-input), select { min-height: 38px; border: 1px solid var(--line); border-radius: 6px; padding: 6px 9px; background: white; color: var(--ink); }
.bulk :deep(uni-input) { width: 190px; }
.bulk button, .pager button, td button, .message button { width: auto; min-height: 36px; padding: 5px 10px; }
.table-scroll { overflow: auto; max-height: min(65vh, 720px); border: 1px solid var(--line); }
table { width: 100%; min-width: 1120px; border-collapse: collapse; font-size: 13px; }
th { position: sticky; top: 0; background: #f4f7f4; z-index: 1; text-align: left; }
th, td { padding: 9px; border-bottom: 1px solid var(--line); vertical-align: top; }
td small { display: block; margin-top: 4px; color: var(--muted); }
td button { display: block; margin: 0 0 5px; }
.numeric { font-variant-numeric: tabular-nums; text-align: right; }
.pending { color: #a45d13; }
.pager { justify-content: flex-end; padding-top: 12px; }
.message { padding: 9px; background: #e6f1e9; margin: 8px 0; }
.message.error { background: #fff1e7; }
.mobile-note { padding: 20px; background: #fff7e9; }
.overlay { position: fixed; inset: 0; z-index: 20; background: var(--dialog-scrim); display: grid; place-items: center; padding: 16px; }
.preview-card { width: min(100%, 980px); height: min(90vh, 900px); display: flex; flex-direction: column; padding: 14px; gap: 8px; }
.preview-head { justify-content: space-between; }
.viewer { flex: 1; min-height: 0; }
</style>
