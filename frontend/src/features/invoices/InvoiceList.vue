<script setup lang="ts">
/**
 * 单据库：一张发票一张卡片。
 *
 * 两条口径（docs/multifiles.md 第 12 节）：
 *
 * 1. **一张发票是一条记录**，不是「一次录入是一条记录」。一次传进来二十张单据就
 *    是二十条。整理批次只是上传时的容器，整理完就没有分组意义了——同一批里混着
 *    好几天、好几家。
 * 2. **两个页签回答两个不同的问题，所以排序和筛选都不一样。**「待处理」是待办
 *    清单：刚录进来的在最前，不分类、不筛，因为还没做完的东西本来就该按「什么时候
 *    进来的」排队。「已完成」是翻历史：那时才需要按采购日期排、按时间范围找。
 */
import { computed, onMounted, ref } from 'vue'
import { errorMessage, exportSelectedInvoices, getInvoices, updateInvoice, voidInvoice, type Invoice } from '../../shared/api'
import { confirmLeave } from '../../shared/platform/leaveGuard'
import { downloadAttachment } from '../../shared/platform/download'
import DateFieldInput from './DateFieldInput.vue'
import InvoiceUpload from './InvoiceUpload.vue'
import SpreadsheetImport from './SpreadsheetImport.vue'
import { isCalendarDay, rangeLabel, rangeQuery } from './invoiceRange'

const emit = defineEmits<{ open: [id: number]; openBatch: [] }>()
const selected = ref<'pending' | 'completed'>('pending')
const items = ref<Invoice[]>([])
const total = ref(0)
const offset = ref(0)
const limit = 20
const state = ref<'loading' | 'ready' | 'error'>('loading')
const failure = ref('')
const uploading = ref(false)
const importing = ref(false)
/** 正在删的那张单据，用来禁掉它自己的按钮。 */
const deletingId = ref<number | null>(null)
/** 删除失败的原因，按单据 id 放，这样只显示在那一张卡上。 */
const deleteError = ref<Record<number, string>>({})
const renamingId = ref<number | null>(null)
const renameText = ref('')
const renamingBusy = ref(false)
const renameError = ref('')
const exportMode = ref(false)
const exportFormat = ref<'csv' | 'xlsx'>('xlsx')
const selectedInvoiceIds = ref<number[]>([])
const exporting = ref(false)
const exportError = ref('')

// --- 「已完成」页签的排序与筛选 ----------------------------------------------
type SortKey = 'invoice_date' | 'created_at'
const SORT_LABEL: Record<SortKey, string> = {
  invoice_date: '按收据日期排序',
  created_at: '按创建时间排序',
}
const sortKey = ref<SortKey>('invoice_date')
const sortMenuOpen = ref(false)
/** 时间范围面板是否展开。范围本身存在 rangeFrom / rangeTo 里。 */
const rangeOpen = ref(false)
const rangeFrom = ref('')
const rangeTo = ref('')
let requestSequence = 0

/** 筛选和排序只在「已完成」里出现，见组件顶部第 2 条。 */
const showFilters = computed(() => selected.value === 'completed')
const rangeActive = computed(() => isCalendarDay(rangeFrom.value) || isCalendarDay(rangeTo.value))
const rangeText = computed(() => rangeLabel(
  isCalendarDay(rangeFrom.value) ? rangeFrom.value : '',
  isCalendarDay(rangeTo.value) ? rangeTo.value : '',
))
const pageLabel = computed(() => total.value === 0
  ? '0 张'
  : `${offset.value + 1}–${Math.min(offset.value + items.value.length, total.value)} / ${total.value} 张`)
const statusLabel: Record<Invoice['status'], string> = {
  draft: '草稿', in_review: '审核中', ready_to_complete: '待完成', completed: '已完成', voided: '已作废'
}

/**
 * 这张单据的原件来自哪里。
 *
 * ⚠️ 批次来的单据，原件挂在整理内容（`intake_sources`）上而不是 `files` 上，所以
 * 只数 `files.length` 会把一份有五页原件的单据显示成「0 个原件」。
 *
 * ⚠️ 只写**出处**（哪个文件的第几页 / 第几行），不写**张数**：「1 个原件」没有回答任何
 * 问题，点进详情就看得到。手工上传的单据因此在列表卡片上不写这一段。
 */
function originLabel(invoice: Invoice): string {
  const sources = invoice.intake_sources
  if (sources.length) {
    const names = new Set(sources.map((source) => source.original_name))
    const name = names.size === 1 ? sources[0].original_name : `${names.size} 个文件`
    if (sources[0].kind === 'row') {
      const rows = sources.map((source) => source.row_no).filter((value) => value !== null)
      return rows.length === 1 ? `${name} 第 ${rows[0]} 行` : `${name} ${sources.length} 行`
    }
    const pages = sources.map((source) => source.page_no).filter((value) => value !== null)
    if (names.size === 1 && pages.length === 1) return `${name} 第 ${pages[0]} 页`
    return `${name} ${sources.length} 页`
  }
  if (invoice.entry_source === 'import') return '表格导入'
  return ''
}

/** `2026-11-23T08:12:30+00:00` → `2026-11-23`：录进系统的那一天。 */
function createdDay(value: string): string {
  return /^\d{4}-\d{2}-\d{2}/.exec(value ?? '')?.[0] ?? value
}

/**
 * 卡片第二行：只写还有信息量的数字。
 *
 * ⚠️ 「待核对 0 · 已入账 2」在**已完成**卡片上恒为噪音：收口本来就要求没有待核对行
 * （`complete_invoice` 缺待办检查会 409），已入账行数点进详情就是正文本身。所以按值写——
 * 待办数只在真有待办时出现（已完成卡片因此永远看不到它），跳过数只在真跳过时出现。
 */
/**
 * 卡片第二行：这张单据在**纸面**上的身份与原件出处。
 *
 * ⚠️ 不写 `invoice.id`。那是库里的主键，只用在 URL（`?view=review&invoice=27`）、
 * 接口和日志里；卡片要回答的是「这是哪张纸」，答案是纸面单号与它在哪个文件第几页。
 */
function paperLabel(invoice: Invoice): string {
  const parts: string[] = []
  if (invoice.invoice_number) parts.push(`单号 ${invoice.invoice_number}`)
  const origin = originLabel(invoice)
  if (origin) parts.push(origin)
  return parts.join(' · ')
}

function countsLabel(invoice: Invoice): string {
  const parts: string[] = []
  if (invoice.reconciliation.pending_count) parts.push(`待核对 ${invoice.reconciliation.pending_count}`)
  if (invoice.reconciliation.skipped_count) parts.push(`已跳过 ${invoice.reconciliation.skipped_count}`)
  parts.push(`创建于 ${createdDay(invoice.uploaded_at)}`)
  return parts.join(' · ')
}

function buildQuery(): string {
  const base = `?completed=${selected.value === 'completed'}&limit=${limit}&offset=${offset.value}`
  if (!showFilters.value) return `${base}&order=created_at`
  return `${base}&order=${sortKey.value}` + rangeQuery(rangeFrom.value, rangeTo.value)
}

async function load() {
  const sequence = ++requestSequence
  state.value = 'loading'
  failure.value = ''
  try {
    const page = await getInvoices(buildQuery())
    if (sequence !== requestSequence) return
    items.value = page.items
    total.value = page.total
    state.value = 'ready'
  } catch (error) {
    if (sequence !== requestSequence) return
    items.value = []
    state.value = 'error'
    failure.value = errorMessage(error)
  }
}

function selectTab(tab: 'pending' | 'completed') {
  if (selected.value === tab) return
  selected.value = tab
  offset.value = 0
  sortMenuOpen.value = false
  void load()
}

function chooseSort(key: SortKey) {
  sortMenuOpen.value = false
  if (sortKey.value === key) return
  sortKey.value = key
  offset.value = 0
  void load()
}

function toggleRangePanel() {
  rangeOpen.value = !rangeOpen.value
  sortMenuOpen.value = false
}

/** 日期框三格填满（且是真实日历日）才重新查；清空一端表示那一端不限。 */
function rangeTyped(which: 'from' | 'to', value: string) {
  const box = which === 'from' ? rangeFrom : rangeTo
  if (value === box.value) return
  if (value && !isCalendarDay(value)) { box.value = value; return }
  box.value = value
  offset.value = 0
  void load()
}

function clearRange() {
  if (!rangeFrom.value && !rangeTo.value) return
  rangeFrom.value = ''
  rangeTo.value = ''
  offset.value = 0
  void load()
}

function showCompleted() {
  selected.value = 'completed'
  offset.value = 0
  void load()
}

async function beginExport(format: 'csv' | 'xlsx') {
  if (!await confirmLeave()) return
  exportFormat.value = format
  exportMode.value = true
  exportError.value = ''
  importing.value = false
  selected.value = 'completed'
  offset.value = 0
  rangeFrom.value = ''
  rangeTo.value = ''
  rangeOpen.value = false
  sortMenuOpen.value = false
  void load()
}

function toggleExport(invoice: Invoice) {
  if (!exportMode.value || invoice.status !== 'completed') return
  const ids = selectedInvoiceIds.value
  if (!ids.includes(invoice.id) && ids.length >= 200) {
    exportError.value = '一次最多选择 200 张单据，请先下载这一批。'
    return
  }
  selectedInvoiceIds.value = ids.includes(invoice.id)
    ? ids.filter((id) => id !== invoice.id) : [...ids, invoice.id]
  exportError.value = ''
}

function cancelExport() {
  exportMode.value = false
  selectedInvoiceIds.value = []
  exportError.value = ''
}

async function downloadSelected() {
  if (exporting.value || selectedInvoiceIds.value.length === 0) return
  exporting.value = true
  exportError.value = ''
  try {
    const response = await exportSelectedInvoices(exportFormat.value, selectedInvoiceIds.value)
    downloadAttachment(response, `selected-invoices.${exportFormat.value}`)
    cancelExport()
  } catch (error) {
    exportError.value = errorMessage(error)
  } finally { exporting.value = false }
}

function showUpload() {
  importing.value = false
  uploading.value = true
}

function changePage(delta: number) {
  offset.value = Math.max(0, offset.value + delta * limit)
  void load()
}

function startRename(invoice: Invoice) {
  renamingId.value = invoice.id
  renameText.value = invoice.source_name ?? ''
  renameError.value = ''
}

async function saveRename(id: number) {
  if (renamingBusy.value) return
  renamingBusy.value = true
  renameError.value = ''
  try {
    const updated = await updateInvoice(id, { source_name: renameText.value.trim() || null })
    items.value = items.value.map((item) => item.id === id ? updated : item)
    renamingId.value = null
  } catch (error) {
    renameError.value = errorMessage(error)
  } finally { renamingBusy.value = false }
}

/**
 * 删掉一张单据。
 *
 * 已入账时再次确认；服务端核对行 ID，并连同单据作废对应采购记录。
 */
async function removeInvoice(invoice: Invoice) {
  if (deletingId.value !== null) return
  const name = invoice.source_name ? `${invoice.source_name}（单据 #${invoice.id}）` : `单据 #${invoice.id}`
  const confirmed = await new Promise<boolean>((resolve) => uni.showModal({
    title: '删除这张单据',
    content: `${name}会连同上传的原件一起删掉。同一个文件之后可以重新上传。`,
    confirmText: '删除', cancelText: '取消',
    success: (result) => resolve(Boolean(result.confirm)), fail: () => resolve(false)
  }))
  if (!confirmed) return
  const bookedIds = invoice.lines.filter((line) => line.status === 'confirmed').map((line) => line.id)
  if (bookedIds.length) {
    const confirmedBooked = await new Promise<boolean>((resolve) => uni.showModal({
      title: '确认移除已入账数据',
      content: `这张单据有 ${bookedIds.length} 行已经入账。确认删除后，这些采购记录会作废，不再计入价格统计；原件也会删除。是否确认移除？`,
      confirmText: '确认移除', cancelText: '保留单据',
      success: (result) => resolve(Boolean(result.confirm)), fail: () => resolve(false)
    }))
    if (!confirmedBooked) return
  }
  deletingId.value = invoice.id
  delete deleteError.value[invoice.id]
  try {
    await voidInvoice(invoice.id, bookedIds.length ? bookedIds : undefined)
    selectedInvoiceIds.value = selectedInvoiceIds.value.filter((id) => id !== invoice.id)
    // 删完这一页可能空了（比如删的是本页唯一一条），往前退一页再读。
    if (items.value.length === 1 && offset.value > 0) offset.value -= limit
    await load()
  } catch (error) {
    deleteError.value = { ...deleteError.value, [invoice.id]: errorMessage(error) }
  } finally {
    deletingId.value = null
  }
}

/** A finished upload goes straight to the invoice it just created. */
function uploaded(invoiceId: number) {
  uploading.value = false
  importing.value = false
  void load()
  emit('open', invoiceId)
}

async function toggleImport() {
  if ((importing.value || uploading.value) && !await confirmLeave()) return
  importing.value = !importing.value
  uploading.value = false
}

async function toggleUpload() {
  if ((importing.value || uploading.value) && !await confirmLeave()) return
  uploading.value = !uploading.value
  importing.value = false
}

onMounted(() => void load())
defineExpose({ refresh: load, showCompleted, showUpload })
</script>

<template>
  <view class="list-layout">
    <view class="page-heading">
      <view><text class="eyebrow">WORKSPACE / 单据</text><text class="page-title">单据</text><text class="muted">查看待处理与已完成的采购单据</text></view>
      <view class="heading-actions"><button class="ui-button secondary" @click="emit('openBatch')">混合文件整理 / 接着整理</button><button class="ui-button secondary" :aria-expanded="importing" @click="toggleImport">{{ importing ? '收起表格' : '导入表格 / 导出明细' }}</button><button class="ui-button" :aria-expanded="uploading" @click="toggleUpload">{{ uploading ? '收起上传' : '上传单据' }}</button></view>
    </view>

    <InvoiceUpload v-if="uploading" mode="create" @uploaded="uploaded" @open-existing="uploaded" @open-batch="emit('openBatch')" @close="uploading = false" />
    <SpreadsheetImport v-if="importing" @created="uploaded" @open-existing="uploaded" @select-export="beginExport" @close="importing = false" />

    <view class="tabs" aria-label="单据状态">
      <button :class="{ active: selected === 'pending' }" :aria-current="selected === 'pending' ? 'page' : undefined" @click="selectTab('pending')">待处理</button>
      <button :class="{ active: selected === 'completed' }" :aria-current="selected === 'completed' ? 'page' : undefined" @click="selectTab('completed')">已完成</button>
    </view>

    <view v-if="exportMode" class="export-bar" role="group" aria-label="导出单据">
      <text>{{ selected === 'completed' ? '勾选下面要导出的已完成单据。' : '待处理单据不能勾选导出，请切到「已完成」。' }}已选 {{ selectedInvoiceIds.length }} 张 · {{ exportFormat.toUpperCase() }}</text>
      <button class="ui-button" :disabled="selectedInvoiceIds.length === 0 || exporting" @click="downloadSelected">{{ exporting ? '正在下载…' : `下载所选（${selectedInvoiceIds.length}）` }}</button>
      <button class="ui-button secondary" :disabled="exporting" @click="cancelExport">取消</button>
      <text v-if="exportError" class="card-error" role="alert">{{ exportError }}</text>
    </view>

    <!-- 排序与筛选只在「已完成」出现：待办清单按录入次序排队就够了。 -->
    <view v-if="showFilters" class="filter-bar" role="group" aria-label="排序与筛选">
      <view class="sort">
        <button class="sort-toggle" :aria-expanded="sortMenuOpen" @click="sortMenuOpen = !sortMenuOpen">
          {{ SORT_LABEL[sortKey] }}<text class="caret" aria-hidden="true">▾</text>
        </button>
        <view v-if="sortMenuOpen" class="sort-menu" role="menu">
          <button v-for="key in (['invoice_date', 'created_at'] as const)" :key="key" class="sort-item"
            role="menuitemradio" :aria-checked="sortKey === key" @click="chooseSort(key)">
            {{ SORT_LABEL[key] }}
          </button>
        </view>
      </view>

      <button class="chip" :class="{ active: rangeActive }" :aria-expanded="rangeOpen" @click="toggleRangePanel">
        {{ rangeActive ? rangeText : '选定时间范围' }}
      </button>
    </view>

    <view v-if="showFilters && rangeOpen" class="range-panel">
      <DateFieldInput :model-value="rangeFrom" label="开始" @update:model-value="rangeTyped('from', $event)" />
      <text class="muted" aria-hidden="true">至</text>
      <DateFieldInput :model-value="rangeTo" label="结束" @update:model-value="rangeTyped('to', $event)" />
      <button class="ui-button secondary small" :disabled="!rangeFrom && !rangeTo" @click="clearRange">清除</button>
      <text class="muted range-note">按单据上的采购日期筛，不是录入时间。留空一端表示那一端不限。</text>
    </view>

    <view v-if="state === 'loading'" class="state-card" role="status" aria-live="polite">正在加载单据…</view>
    <view v-else-if="state === 'error'" class="state-card" role="alert"><text>单据加载失败：{{ failure }}</text><button class="ui-button secondary" @click="load">重试</button></view>
    <view v-else-if="items.length === 0" class="state-card" role="status">
      <text v-if="showFilters && rangeActive">{{ rangeText }}没有已完成单据。</text>
      <text v-else>{{ selected === 'pending' ? '暂无待处理单据' : '暂无已完成单据' }}</text>
      <button v-if="showFilters && rangeActive" class="ui-button secondary" @click="clearRange">看全部</button>
      <button v-else-if="selected === 'pending' && !uploading" class="ui-button" @click="uploading = true">上传单据</button>
      <text v-else-if="selected === 'completed'" class="muted">完成审核的单据会显示在这里。</text>
    </view>
    <template v-else>
      <!-- id 不再出现在卡片文字里；浏览器实跑仍要认出每张卡片是哪一条，所以留在 data 上。 -->
      <view v-for="invoice in items" :key="invoice.id" class="ui-card invoice-card" :data-invoice-id="invoice.id">
        <button v-if="exportMode" class="export-check" role="checkbox"
          :aria-label="invoice.status === 'completed' ? `导出${invoice.source_name || '单据'}${invoice.invoice_number ? `（单号 ${invoice.invoice_number}）` : ''}` : '待处理单据不能导出'"
          :aria-checked="selectedInvoiceIds.includes(invoice.id)"
          :disabled="invoice.status !== 'completed'"
          :title="invoice.status === 'completed' ? '勾选导出这张单据' : '待处理单据不能导出'"
          @click="toggleExport(invoice)"><text class="export-box" aria-hidden="true">{{ selectedInvoiceIds.includes(invoice.id) ? '✓' : '' }}</text></button>
        <view class="card-main">
          <text class="card-tag">{{ statusLabel[invoice.status] }}</text>
          <view class="title-row">
            <text class="card-title">{{ invoice.source_name || '来源待填' }}</text>
            <button v-if="renamingId !== invoice.id" class="rename-button" @click="startRename(invoice)">重命名</button>
          </view>
          <view v-if="renamingId === invoice.id" class="rename-form">
            <input v-model="renameText" class="ui-input" aria-label="单据来源名称" placeholder="输入单据名称" maxlength="120" @confirm="saveRename(invoice.id)" />
            <button class="ui-button" :disabled="renamingBusy" @click="saveRename(invoice.id)">保存</button>
            <button class="ui-button secondary" :disabled="renamingBusy" @click="renamingId = null">取消</button>
            <text v-if="invoice.status === 'completed'" class="muted rename-note">已入账采购记录的来源名称不会随之改变。</text>
            <text v-if="renameError" class="card-error" role="alert">{{ renameError }}</text>
          </view>
          <text class="invoice-date">单据发生日期：{{ invoice.invoice_date || '待填' }}</text>
          <text v-if="paperLabel(invoice)" class="muted">{{ paperLabel(invoice) }}</text>
          <text class="counts">{{ countsLabel(invoice) }}</text>
        </view>
        <view class="card-actions">
          <button class="ui-button secondary" @click="emit('open', invoice.id)">查看详情</button>
          <button class="ui-button ghost danger" :disabled="deletingId === invoice.id"
            @click="removeInvoice(invoice)">{{ deletingId === invoice.id ? '删除中…' : '删除' }}</button>
          <text v-if="deleteError[invoice.id]" class="card-error" role="alert">{{ deleteError[invoice.id] }}</text>
        </view>
      </view>
    </template>
    <view v-if="state === 'ready' && total > 0" class="pagination">
      <text>{{ pageLabel }}</text>
      <view class="page-actions"><button class="ui-button secondary" :disabled="offset === 0" @click="changePage(-1)">上一页</button><button class="ui-button secondary" :disabled="offset + items.length >= total" @click="changePage(1)">下一页</button></view>
    </view>
  </view>
</template>

<style scoped>
.list-layout { display: flex; flex-direction: column; gap: 18px; }
.page-heading { display: flex; align-items: end; justify-content: space-between; gap: 16px; }
.heading-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.page-title { display: block; margin: 8px 0 6px; font-size: 30px; font-weight: 700; }
.tabs { display: flex; gap: 24px; border-bottom: 1px solid var(--line); }
.tabs button { min-height: 44px; margin: 0; padding: 8px 4px; border: 0; border-bottom: 3px solid transparent; border-radius: 0; background: transparent; color: var(--muted); font-size: 15px; cursor: pointer; }
.tabs button::after { border: 0; }
.tabs button.active { border-color: var(--green); color: var(--ink); font-weight: 700; }
.export-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; padding: 14px 16px; border: 1px solid var(--line); border-radius: 9px; background: #f7faf7; }
.export-bar > text:first-child { flex: 1 1 260px; }
.export-bar .card-error { flex-basis: 100%; }

.filter-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: -6px; }
.sort { position: relative; }
.sort-toggle { min-height: 40px; margin: 0; padding: 7px 14px; display: flex; align-items: center; gap: 8px; border: 1px solid var(--line); border-radius: 8px; background: #fff; color: var(--ink); font-size: 14px; font-weight: 650; cursor: pointer; }
.sort-toggle::after { border: 0; }
.caret { color: var(--muted); font-size: 12px; }
.sort-menu { position: absolute; z-index: 20; top: calc(100% + 6px); left: 0; min-width: 180px; display: flex; flex-direction: column; padding: 4px; border: 1px solid var(--line); border-radius: 9px; background: #fff; box-shadow: 0 8px 24px rgb(0 0 0 / 12%); }
.sort-item { min-height: 40px; margin: 0; padding: 8px 12px; border: 0; border-radius: 6px; background: transparent; color: var(--ink); font-size: 14px; text-align: left; cursor: pointer; }
.sort-item::after { border: 0; }
.sort-item[aria-checked="true"] { color: var(--green); background: #eef5ef; font-weight: 650; }
.chip { min-height: 40px; margin: 0; padding: 7px 14px; border: 1px solid var(--line); border-radius: 999px; background: transparent; color: var(--muted); font-size: 14px; line-height: 1.4; cursor: pointer; }
.chip::after { border: 0; }
.chip.active { border-color: var(--green); color: var(--green); background: #eef5ef; font-weight: 650; }
.range-panel { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; padding: 14px 16px; border: 1px solid var(--line); border-radius: 9px; background: #fafbfa; }
.range-panel .small { min-height: 40px; padding: 6px 14px; font-size: 13px; }
.range-note { flex-basis: 100%; font-size: 13px; }

.state-card { min-height: 140px; display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 12px; padding: 24px; border: 1px dashed var(--line); border-radius: 9px; text-align: center; }
.invoice-card { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 20px 24px; }
.export-check { flex: none; display: grid; place-items: center; width: 44px; min-width: 44px; height: 44px; min-height: 44px; margin: 0; padding: 0; border: 0; background: transparent; cursor: pointer; }
.export-check::after { border: 0; }
.export-check:disabled { opacity: .45; cursor: not-allowed; }
.export-box { display: grid; place-items: center; width: 22px; height: 22px; border: 2px solid var(--green); border-radius: 4px; background: #fff; color: #fff; font-size: 16px; line-height: 1; }
.export-check[aria-checked="true"] .export-box { background: var(--green); }
.card-main { min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 7px; }
.card-tag { padding: 4px 8px; border-radius: 5px; color: var(--warning); background: #fff5e9; font-size: 12px; font-weight: 700; }
.card-title { overflow-wrap: anywhere; font-size: 18px; font-weight: 650; }
/* 日期是这张卡片最该先看到的事实，所以用正文色而不是 .muted 的灰。 */
.invoice-date { font-size: 15px; font-weight: 650; }
.title-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.rename-button { min-height: 44px; margin: 0; padding: 6px 8px; border: 0; background: transparent; color: var(--green); font-size: 13px; cursor: pointer; }
.rename-button::after { border: 0; }
.rename-form { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; max-width: 100%; }
.rename-form .ui-input { width: min(300px, 100%); }
.rename-note { flex-basis: 100%; font-size: 12px; }
.counts { font-size: 13px; }
.card-actions { flex: none; display: flex; flex-direction: column; align-items: stretch; gap: 8px; max-width: 220px; }
.card-actions .ui-button.danger { color: var(--error); }
.card-error { color: var(--error); font-size: 13px; line-height: 1.5; }
.pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: var(--muted); font-size: 14px; }
.page-actions { display: flex; gap: 8px; }
.page-actions .ui-button { min-width: 84px; }
@media (max-width: 899px) { .page-heading { align-items: start; flex-direction: column; }.heading-actions { width: 100%; }.heading-actions .ui-button { flex: 1; padding: 8px; font-size: 12px; }.page-title { font-size: 26px; }.invoice-card { align-items: stretch; flex-direction: column; padding: 18px; }.invoice-card .ui-button { width: 100%; }.card-actions { max-width: none; } }
</style>
