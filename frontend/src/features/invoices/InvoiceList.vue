<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { errorMessage, getInvoices, updateInvoice, voidInvoice, type Invoice } from '../../shared/api'
import { confirmLeave } from '../../shared/platform/leaveGuard'
import InvoiceUpload from './InvoiceUpload.vue'
import SpreadsheetImport from './SpreadsheetImport.vue'

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
let requestSequence = 0

const pageLabel = computed(() => total.value === 0 ? '0 项' : `${offset.value + 1}–${Math.min(offset.value + items.value.length, total.value)} / ${total.value} 项`)
const statusLabel: Record<Invoice['status'], string> = {
  draft: '草稿', in_review: '审核中', ready_to_complete: '待完成', completed: '已完成', voided: '已作废'
}

async function load() {
  const sequence = ++requestSequence
  state.value = 'loading'
  failure.value = ''
  try {
    const page = await getInvoices(`?completed=${selected.value === 'completed'}&limit=${limit}&offset=${offset.value}`)
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
  void load()
}

function showCompleted() {
  selected.value = 'completed'
  offset.value = 0
  void load()
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
    <SpreadsheetImport v-if="importing" @created="uploaded" @open-existing="uploaded" @close="importing = false" />

    <view class="tabs" aria-label="单据状态">
      <button :class="{ active: selected === 'pending' }" :aria-current="selected === 'pending' ? 'page' : undefined" @click="selectTab('pending')">待处理</button>
      <button :class="{ active: selected === 'completed' }" :aria-current="selected === 'completed' ? 'page' : undefined" @click="selectTab('completed')">已完成</button>
    </view>
    <view v-if="state === 'loading'" class="state-card" role="status" aria-live="polite">正在加载单据…</view>
    <view v-else-if="state === 'error'" class="state-card" role="alert"><text>单据加载失败：{{ failure }}</text><button class="ui-button secondary" @click="load">重试</button></view>
    <view v-else-if="items.length === 0" class="state-card" role="status">
      <text>{{ selected === 'pending' ? '暂无待处理单据' : '暂无已完成单据' }}</text>
      <button v-if="selected === 'pending' && !uploading" class="ui-button" @click="uploading = true">上传单据</button>
      <text v-else-if="selected === 'completed'" class="muted">完成审核的单据会显示在这里。</text>
    </view>
    <template v-else>
      <view v-for="invoice in items" :key="invoice.id" class="ui-card invoice-card">
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
          <text class="muted">单据 #{{ invoice.id }} · {{ invoice.invoice_date || '日期待填' }} · {{ invoice.entry_source === 'import' ? '表格导入' : '文件单据' }} · {{ invoice.files.length }} 个原件</text>
          <text class="counts">待核对 {{ invoice.reconciliation.pending_count }} · 已入账 {{ invoice.reconciliation.confirmed_count }} · 已跳过 {{ invoice.reconciliation.skipped_count }}</text>
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
.state-card { min-height: 140px; display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 12px; padding: 24px; border: 1px dashed var(--line); border-radius: 9px; text-align: center; }
.invoice-card { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 20px 24px; }
.card-main { min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 7px; }
.card-tag { padding: 4px 8px; border-radius: 5px; color: var(--warning); background: #fff5e9; font-size: 12px; font-weight: 700; }
.card-title { overflow-wrap: anywhere; font-size: 18px; font-weight: 650; }
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
