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
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { errorMessage, exportSelectedInvoices, getInvoiceMonths, getInvoices, updateInvoice, voidInvoice, type Invoice, type InvoiceMonth } from '../../shared/api'
import { confirmLeave } from '../../shared/platform/leaveGuard'
import { downloadAttachment } from '../../shared/platform/download'
import DateFieldInput from './DateFieldInput.vue'
import InvoiceUpload from './InvoiceUpload.vue'
import InvoiceIconPreview from './InvoiceIconPreview.vue'
import { invoiceIconKind } from './invoiceIcon'
import SpreadsheetImport from './SpreadsheetImport.vue'
import { isCalendarDay, rangeLabel, rangeQuery } from './invoiceRange'

const emit = defineEmits<{ open: [id: number]; openBatch: [] }>()
const selected = ref<'pending' | 'completed'>('pending')
const items = ref<Invoice[]>([])
const months = ref<InvoiceMonth[]>([])
const activeMonth = ref('')
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
let monthJumpLockUntil = 0

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

function monthOf(invoice: Invoice): string {
  const value = selected.value === 'completed' && sortKey.value === 'invoice_date'
    ? invoice.invoice_date : invoice.uploaded_at
  return value?.slice(0, 7) || 'undated'
}
function monthLabel(key: string): string {
  if (key === 'undated') return '日期待填'
  const [year, month] = key.split('-')
  return `${year} 年 ${Number(month)} 月`
}
const pageMonths = computed(() => {
  const groups: { key: string; items: Invoice[] }[] = []
  for (const invoice of items.value) {
    const key = monthOf(invoice)
    const last = groups[groups.length - 1]
    if (last?.key === key) last.items.push(invoice)
    else groups.push({ key, items: [invoice] })
  }
  return groups
})

function totalLabel(invoice: Invoice): string {
  if (invoice.invoice_total === null) return ''
  const negative = invoice.invoice_total.startsWith('-')
  const amount = (negative ? invoice.invoice_total.slice(1) : invoice.invoice_total)
    .replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const sign = negative ? '−' : ''
  if (invoice.currency === 'USD') return `${sign}$${amount}`
  if (invoice.currency === 'CNY') return `${sign}¥${amount}`
  return `${sign}${invoice.currency ? `${invoice.currency} ` : ''}${amount}`
}

function buildQuery(): string {
  const base = `?completed=${selected.value === 'completed'}&limit=${limit}&offset=${offset.value}`
  if (!showFilters.value) return `${base}&order=created_at`
  return `${base}&order=${sortKey.value}` + rangeQuery(rangeFrom.value, rangeTo.value)
}

function buildMonthQuery(): string {
  const base = `?completed=${selected.value === 'completed'}&order=${showFilters.value ? sortKey.value : 'created_at'}`
  return showFilters.value ? base + rangeQuery(rangeFrom.value, rangeTo.value) : base
}

async function load() {
  const sequence = ++requestSequence
  state.value = 'loading'
  failure.value = ''
  try {
    const [page, index] = await Promise.all([getInvoices(buildQuery()), getInvoiceMonths(buildMonthQuery())])
    if (sequence !== requestSequence) return
    items.value = page.items
    months.value = index.months
    activeMonth.value = page.items.length ? monthOf(page.items[0]!) : ''
    total.value = page.total
    state.value = 'ready'
  } catch (error) {
    if (sequence !== requestSequence) return
    items.value = []
    months.value = []
    state.value = 'error'
    failure.value = errorMessage(error)
  }
}

async function jumpToMonth(month: InvoiceMonth) {
  const pageHasMonth = pageMonths.value.some((group) => group.key === month.key)
  if (!pageHasMonth) {
    offset.value = month.offset
    await load()
  }
  await nextTick()
  activeMonth.value = month.key
  monthJumpLockUntil = Date.now() + 450
  uni.pageScrollTo({ selector: `#invoice-month-${month.key}`, duration: 240 })
}

function syncActiveMonth() {
  // #ifdef H5
  if (Date.now() < monthJumpLockUntil) return
  const headings = Array.from(document.querySelectorAll<HTMLElement>('.month-section[data-month-key]'))
  let visible = headings[0]
  for (const heading of headings) {
    if (heading.getBoundingClientRect().top <= window.innerHeight * 0.3) visible = heading
  }
  if (visible?.dataset.monthKey) activeMonth.value = visible.dataset.monthKey
  // #endif
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
    confirmColor: '#9a3b2f',
    success: (result) => resolve(Boolean(result.confirm)), fail: () => resolve(false)
  }))
  if (!confirmed) return
  const bookedIds = invoice.lines.filter((line) => line.status === 'confirmed').map((line) => line.id)
  if (bookedIds.length) {
    const confirmedBooked = await new Promise<boolean>((resolve) => uni.showModal({
      title: '确认移除已入账数据',
      content: `这张单据有 ${bookedIds.length} 行已经入账。确认删除后，这些采购记录会作废，不再计入价格统计；原件也会删除。是否确认移除？`,
      confirmText: '确认移除', cancelText: '保留单据',
      confirmColor: '#9a3b2f',
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

onMounted(() => {
  void load()
  // #ifdef H5
  window.addEventListener('scroll', syncActiveMonth, { passive: true })
  // #endif
})
onUnmounted(() => {
  // #ifdef H5
  window.removeEventListener('scroll', syncActiveMonth)
  // #endif
})
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

    <!-- 已完成页签保留排序和日期筛选。 -->
    <view v-if="showFilters" class="list-toolbar" role="group" aria-label="单据筛选工具栏">
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
    <view v-else class="month-layout" aria-label="按月份排列的单据">
      <view class="month-stream">
      <view v-for="group in pageMonths" :id="`invoice-month-${group.key}`" :key="group.key" class="month-section" :data-month-key="group.key">
        <view class="month-heading"><text>{{ monthLabel(group.key) }}</text><view aria-hidden="true"></view></view>
        <view class="icon-grid" aria-label="单据">
      <view v-for="invoice in group.items" :key="invoice.id" class="ui-card icon-card" :class="`icon-${invoiceIconKind(invoice)}`" :data-invoice-id="invoice.id">
        <view class="icon-heading">
          <button v-if="exportMode" class="export-check" role="checkbox"
            :aria-label="invoice.status === 'completed' ? `导出${invoice.source_name || '单据'}` : '待处理单据不能导出'"
            :aria-checked="selectedInvoiceIds.includes(invoice.id)" :disabled="invoice.status !== 'completed'"
            @click="toggleExport(invoice)"><text class="export-box" aria-hidden="true">{{ selectedInvoiceIds.includes(invoice.id) ? '✓' : '' }}</text></button>
          <text class="card-tag">{{ statusLabel[invoice.status] }}</text>
          <text class="icon-date">{{ invoice.invoice_date || '日期待填' }}</text>
        </view>
        <button class="icon-open" :aria-label="`查看${invoice.source_name || '单据'}的详情`" @click="emit('open', invoice.id)">
          <InvoiceIconPreview :invoice="invoice" />
        </button>
        <view class="icon-details">
          <text class="icon-title" :title="invoice.source_name || '来源待填'">{{ invoice.source_name || '来源待填' }}</text>
          <text v-if="invoice.invoice_total !== null" class="icon-subtitle" :title="`总金额 ${totalLabel(invoice)}`">总金额 {{ totalLabel(invoice) }}</text>
          <text v-if="invoice.reconciliation.pending_count" class="icon-review-note">待核对 {{ invoice.reconciliation.pending_count }}</text>
          <view v-if="renamingId === invoice.id" class="icon-rename">
            <input v-model="renameText" class="ui-input" aria-label="单据来源名称" placeholder="输入单据名称" maxlength="120" @confirm="saveRename(invoice.id)" />
            <button class="ui-button" :disabled="renamingBusy" @click="saveRename(invoice.id)">保存</button>
            <button class="ui-button secondary" :disabled="renamingBusy" @click="renamingId = null">取消</button>
            <text v-if="invoice.status === 'completed'" class="muted rename-note">已入账采购记录的来源名称不会随之改变。</text>
            <text v-if="renameError" class="card-error" role="alert">{{ renameError }}</text>
          </view>
          <view v-else class="icon-actions">
            <button @click="emit('open', invoice.id)">查看详情</button>
            <button @click="startRename(invoice)">重命名</button>
            <button class="danger" :disabled="deletingId === invoice.id" @click="removeInvoice(invoice)">{{ deletingId === invoice.id ? '删除中…' : '删除' }}</button>
          </view>
          <text v-if="deleteError[invoice.id]" class="card-error" role="alert">{{ deleteError[invoice.id] }}</text>
        </view>
      </view>
        </view>
      </view>
      </view>
      <view v-if="months.length" class="month-rail" role="navigation" aria-label="月份时间轴">
        <button v-for="month in months" :key="month.key" class="month-tick"
          :class="{ active: activeMonth === month.key }" :aria-label="`跳到 ${monthLabel(month.key)}，${month.count} 张单据`"
          :aria-current="activeMonth === month.key ? 'location' : undefined" :title="`${monthLabel(month.key)} · ${month.count} 张`"
          @click="jumpToMonth(month)"><text class="tick-line" aria-hidden="true"></text><text class="tick-label">{{ monthLabel(month.key) }}</text></button>
      </view>
    </view>
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

.list-toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: -6px; }
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
.month-layout { position: relative; padding-right: 125px; }
.month-stream { display: flex; flex-direction: column; gap: 30px; }
.month-section { scroll-margin-top: 24px; }
.month-heading { display: flex; align-items: center; gap: 16px; margin: 0 0 16px; color: var(--ink); font-size: 18px; font-weight: 700; }
.month-heading > view { flex: 1; border-top: 1px solid var(--line); }
.month-rail { position: fixed; z-index: 10; top: 50%; right: max(14px, calc((100vw - 1440px) / 2 + 10px)); transform: translateY(-50%); display: flex; flex-direction: column; align-items: flex-end; max-height: 70vh; overflow-y: auto; padding: 8px 2px; border-right: 2px solid var(--line); background: color-mix(in srgb, var(--canvas) 88%, transparent); }
.month-tick { display: flex; align-items: center; justify-content: flex-end; gap: 7px; width: 115px; min-height: 36px; margin: 0; padding: 4px 0; border: 0; background: transparent; color: var(--muted); cursor: pointer; }
.month-tick::after { border: 0; }
.tick-line { display: block; width: 18px; height: 3px; border-radius: 3px; background: var(--muted); transition: width .15s ease, background .15s ease; }
.tick-label { order: -1; visibility: hidden; white-space: nowrap; font-size: 12px; }
.month-tick.active { color: var(--green); }
.month-tick.active .tick-line, .month-tick:hover .tick-line, .month-tick:focus-visible .tick-line { width: 34px; background: var(--green); }
.month-tick.active .tick-label, .month-tick:hover .tick-label, .month-tick:focus-visible .tick-label { visibility: visible; }
.icon-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 18px; align-items: stretch; }
.icon-card { --type-color: #60726b; position: relative; min-width: 0; padding: 14px; display: flex; flex-direction: column; gap: 9px; overflow: hidden; border: 2px solid var(--type-color); border-radius: 11px; background: #fff; color: var(--type-color); }
.icon-card.icon-pdf { --type-color: #ba4f42; border-radius: 11px 35px 11px 11px; background: #fff9f7; }
.icon-card.icon-pdf::after { content: ''; position: absolute; top: -2px; right: -2px; width: 26px; height: 26px; border-left: 2px solid var(--type-color); border-bottom: 2px solid var(--type-color); border-radius: 0 0 0 6px; background: #fff; pointer-events: none; }
.icon-card.icon-table { --type-color: #25805d; border-radius: 7px; background: #f6fbf7; }
.icon-card.icon-photo { --type-color: #7650a4; border-radius: 14px; background: #faf7ff; }
.icon-card.icon-table::before, .icon-card.icon-photo::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 6px; background: var(--type-color); }
.icon-card.icon-photo::before { background: linear-gradient(90deg, #7650a4 35%, #b89bd6 35%); }
.icon-heading { min-height: 30px; display: flex; align-items: center; gap: 7px; }
.icon-pdf .icon-heading { padding-right: 24px; }
.icon-heading .export-check { width: 30px; min-width: 30px; height: 30px; min-height: 30px; }
.icon-date { margin-left: auto; color: var(--ink); font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }
.icon-open { display: block; width: 100%; min-height: 0; margin: 0; padding: 0; border: 0; background: transparent; text-align: left; cursor: pointer; }
.icon-open::after { border: 0; }
.icon-open:focus-visible { outline: 3px solid var(--green); outline-offset: 3px; border-radius: 11px; }
.icon-details { min-width: 0; display: flex; flex: 1; flex-direction: column; gap: 4px; }
.icon-title { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: var(--ink); font-size: 16px; font-weight: 700; }
.icon-subtitle { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: var(--muted); font-size: 12px; }
.icon-review-note { color: var(--warning); font-size: 12px; }
.icon-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: auto; padding-top: 6px; }
.icon-actions button { min-height: 35px; margin: 0; padding: 4px 7px; border: 0; background: transparent; color: var(--green); font-size: 12px; cursor: pointer; }
.icon-actions button::after { border: 0; }
.icon-actions .danger { color: var(--error); }
.icon-rename { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
.icon-rename .ui-input { width: 100%; }
.icon-rename .ui-button { min-height: 35px; padding: 4px 10px; font-size: 12px; }
.export-check { flex: none; display: grid; place-items: center; width: 44px; min-width: 44px; height: 44px; min-height: 44px; margin: 0; padding: 0; border: 0; background: transparent; cursor: pointer; }
.export-check::after { border: 0; }
.export-check:disabled { opacity: .45; cursor: not-allowed; }
.export-box { display: grid; place-items: center; width: 22px; height: 22px; border: 2px solid var(--green); border-radius: 4px; background: #fff; color: #fff; font-size: 16px; line-height: 1; }
.export-check[aria-checked="true"] .export-box { background: var(--green); }
.card-tag { padding: 4px 8px; border-radius: 5px; color: var(--warning); background: #fff5e9; font-size: 12px; font-weight: 700; }
.rename-note { flex-basis: 100%; font-size: 12px; }
.card-error { color: var(--error); font-size: 13px; line-height: 1.5; }
.pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: var(--muted); font-size: 14px; }
.page-actions { display: flex; gap: 8px; }
.page-actions .ui-button { min-width: 84px; }
@media (max-width: 899px) { .page-heading { align-items: start; flex-direction: column; }.heading-actions { width: 100%; }.heading-actions .ui-button { flex: 1; padding: 8px; font-size: 12px; }.page-title { font-size: 26px; } }
@media (max-width: 540px) { .month-layout { padding-right: 46px; }.month-rail { right: 4px; }.month-tick { width: 42px; }.tick-label { display: none; }.icon-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }.icon-card { padding: 8px; }.icon-heading { flex-wrap: wrap; }.icon-date { font-size: 15px; }.icon-actions button { padding: 4px; }.icon-open :deep(.document) { height: 190px; } }
</style>
