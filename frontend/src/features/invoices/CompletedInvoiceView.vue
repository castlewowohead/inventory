<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { errorMessage, getIntakeItemOriginal, getInvoiceFileContent, getPurchase, reopenInvoice, updateInvoice, type Invoice, type InvoiceLine, type Purchase } from '../../shared/api'
import { downloadAttachment } from '../../shared/platform/download'
import OriginalDocumentViewer from './OriginalDocumentViewer.vue'

const props = defineProps<{ invoice: Invoice }>()
const emit = defineEmits<{ back: []; openExisting: [id: number]; currencyChanged: [invoice: Invoice]; reopened: [invoice: Invoice] }>()
const purchases = ref<Record<number, Purchase>>({})
const failedIds = ref<number[]>([])
const loading = ref(false)
const originalOpen = ref(false)
const downloadError = ref('')
const editingCurrency = ref(false)
const currencyChoice = ref<'USD' | 'CNY'>('USD')
const currencySaving = ref(false)
const currencyError = ref('')
const reopening = ref(false)
const reopenError = ref('')
let requestSequence = 0
onUnmounted(() => { requestSequence += 1 })

const confirmed = computed(() => props.invoice.lines.filter((line) => line.status === 'confirmed'))
const skipped = computed(() => props.invoice.lines.filter((line) => line.status === 'skipped'))
const fileCount = computed(() => props.invoice.files.length + props.invoice.intake_sources.length)
const receiptCurrency = computed(() => props.invoice.currency || 'USD')
const hasRevisions = computed(() => Object.values(purchases.value).some((purchase) => purchase.revision_count > 0 || !!purchase.voided_at))

async function loadPurchases() {
  const sequence = ++requestSequence
  purchases.value = {}
  failedIds.value = []
  const ids = [...new Set(confirmed.value.map((line) => line.purchase_id).filter((id): id is number => id !== null))]
  loading.value = ids.length > 0
  const loaded: Record<number, Purchase> = {}
  const failures: number[] = []
  // A large invoice should not send every purchase request at once.
  for (let offset = 0; offset < ids.length; offset += 6) {
    const batch = await Promise.allSettled(ids.slice(offset, offset + 6).map((id) => getPurchase(id)))
    if (sequence !== requestSequence) return
    batch.forEach((result, index) => {
      const id = ids[offset + index]
      if (result.status === 'fulfilled') loaded[id] = result.value
      else failures.push(id)
    })
  }
  if (sequence !== requestSequence) return
  purchases.value = loaded
  failedIds.value = failures
  loading.value = false
}

watch(() => props.invoice, () => { void loadPurchases() }, { immediate: true })

function purchaseFor(line: InvoiceLine): Purchase | null {
  return line.purchase_id === null ? null : purchases.value[line.purchase_id] ?? null
}

function productName(purchase: Purchase): string {
  if (purchase.line_kind === 'adjustment') return purchase.original_description || '调整项'
  return purchase.food_item?.name_zh || purchase.food_item?.name || purchase.original_description || '未命名商品'
}

function description(purchase: Purchase): string | null {
  const value = purchase.original_description?.trim()
  return value && value !== productName(purchase) ? value : null
}

function grouped(value: string): string {
  const [whole, fraction] = value.split('.')
  return `${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${fraction === undefined ? '' : `.${fraction}`}`
}

function money(value: string | null, currency: string | null): string {
  if (value === null) return '未知'
  const sign = value.startsWith('-') ? '−' : ''
  const digits = grouped(sign ? value.slice(1) : value)
  if (currency === 'USD') return `${sign}$${digits}`
  if (currency === 'CNY') return `${sign}¥${digits}`
  return `${sign}${currency ? `${currency} ` : ''}${digits}`
}

function quantity(purchase: Purchase): string {
  if (purchase.quantity === null) return '数量未知'
  return `${grouped(purchase.quantity)}${purchase.purchase_unit ? ` ${purchase.purchase_unit}` : ''}`
}

const reconciliationMessage = computed(() => {
  switch (props.invoice.reconciliation.status) {
    case 'balanced': return ''
    case 'mismatch': return '单据总额与完成时入账金额不一致，请核对原件。'
    case 'no_total': return '单据未填写总额，无法计算差额。'
    case 'mixed_currencies': return '明细含多种币种，无法合计金额。'
    case 'currency_required': return '这张旧单据完成时未记录币种，无法计算差额；此处金额按默认美元展示。'
    case 'currency_mismatch': return '明细币种与单据币种不同，无法计算差额。'
    default: return '当前无法计算单据差额。'
  }
})

async function downloadOriginal(fileId: number, name: string) {
  downloadError.value = ''
  try {
    downloadAttachment(await getInvoiceFileContent(props.invoice.id, fileId), name)
  } catch (error) {
    downloadError.value = errorMessage(error)
  }
}

async function downloadIntakeOriginal(batchId: number, itemId: number, name: string) {
  downloadError.value = ''
  try {
    downloadAttachment(await getIntakeItemOriginal(batchId, itemId), name)
  } catch (error) {
    downloadError.value = errorMessage(error)
  }
}

function beginCurrencyEdit() {
  currencyChoice.value = props.invoice.currency === 'CNY' ? 'CNY' : 'USD'
  currencyError.value = ''
  editingCurrency.value = true
}

async function saveCurrency() {
  if (currencySaving.value) return
  currencySaving.value = true
  currencyError.value = ''
  try {
    const stored = await updateInvoice(props.invoice.id, { currency: currencyChoice.value })
    editingCurrency.value = false
    emit('currencyChanged', stored)
  } catch (error) {
    currencyError.value = errorMessage(error)
  } finally {
    currencySaving.value = false
  }
}

async function reopenForEditing() {
  if (reopening.value || currencySaving.value) return
  reopening.value = true
  reopenError.value = ''
  try {
    emit('reopened', await reopenInvoice(props.invoice.id))
  } catch (error) {
    reopenError.value = errorMessage(error)
  } finally {
    reopening.value = false
  }
}
</script>

<template>
  <view class="receipt-page">
    <button class="back-link" @click="emit('back')">← 返回单据</button>
    <view class="receipt ui-card">
      <view class="receipt-header">
        <view class="supplier-block">
          <text class="eyebrow">采购回执 · 单据 #{{ invoice.id }}</text>
          <text class="field-label">采购来源</text>
          <text class="supplier">{{ invoice.source_name || '采购来源待填' }}</text>
          <view class="metadata">
            <text>日期 {{ invoice.invoice_date || '待填' }}</text>
            <text>单号 {{ invoice.invoice_number || '待填' }}</text>
          </view>
        </view>
        <view class="total-block">
          <view class="badges"><text class="status">已完成</text><text v-if="invoice.recognition.is_fake" class="demo">演示识别结果</text><button class="edit-receipt" :disabled="reopening || currencySaving" @click="reopenForEditing">{{ reopening ? '正在打开…' : '修改' }}</button></view>
          <text v-if="reopenError" class="load-error reopen-error" role="alert">{{ reopenError }}</text>
          <text class="field-label">单据总额</text>
          <text class="grand-total">{{ money(invoice.invoice_total, receiptCurrency) }}</text>
          <button v-if="!editingCurrency" class="currency-edit" @click="beginCurrencyEdit">币种 {{ receiptCurrency }} · 修改</button>
          <view v-else class="currency-form">
            <text>只改单据总额的币种标记，已入账明细不会自动换算或改币种。</text>
            <picker :range="['USD', 'CNY']" :value="currencyChoice === 'CNY' ? 1 : 0" @change="currencyChoice = Number($event.detail.value) === 1 ? 'CNY' : 'USD'">
              <view class="ui-input currency-picker">{{ currencyChoice }} ▾</view>
            </picker>
            <view class="currency-actions"><button class="ui-button" :disabled="currencySaving" @click="saveCurrency">保存</button><button class="ui-button secondary" :disabled="currencySaving" @click="editingCurrency = false">取消</button></view>
            <text v-if="currencyError" class="load-error" role="alert">{{ currencyError }}</text>
          </view>
        </view>
      </view>

      <view class="summary-bar">
        <text>已入账 <strong>{{ invoice.reconciliation.confirmed_count }}</strong> 行</text>
        <text>已跳过 <strong>{{ invoice.reconciliation.skipped_count }}</strong> 行</text>
        <text>原件 <strong>{{ fileCount }}</strong> 份</text>
        <button v-if="fileCount && invoice.entry_source !== 'import'" class="original-button" @click="originalOpen = true">查看原件 ↗</button>
      </view>

      <view v-if="invoice.entry_source === 'import' && fileCount" class="import-files">
        <button v-for="file in invoice.files" :key="file.id" class="ui-button secondary" @click="downloadOriginal(file.id, file.original_name)">下载原始表格 · {{ file.original_name }}</button>
        <button v-for="source in invoice.intake_sources" :key="source.item_id" class="ui-button secondary" @click="downloadIntakeOriginal(source.batch_id, source.item_id, source.original_name)">下载原始表格 · {{ source.original_name }} · 第 {{ source.row_no }} 行</button>
        <text v-if="downloadError" class="load-error" role="alert">{{ downloadError }}</text>
      </view>

      <view v-if="invoice.possible_duplicate_invoice_ids.length" class="duplicate-note" role="status">
        <text>这张单据可能与已有单据重复，请核对。</text>
        <button v-for="id in invoice.possible_duplicate_invoice_ids" :key="id" class="retry-button" @click="emit('openExisting', id)">查看单据 #{{ id }}</button>
      </view>

      <view v-if="reconciliationMessage" class="reconciliation-alert" role="status">{{ reconciliationMessage }}</view>
      <view v-if="hasRevisions" class="revision-note" role="status">下方明细显示当前采购记录；完成时的入账金额保留在回执底部。</view>

      <view class="items-heading"><text>采购明细</text><text class="item-count">{{ confirmed.length }} 项</text></view>
      <view class="table-head" aria-hidden="true"><text>商品 / 记录描述</text><text>数量</text><text>单价</text><text>行金额</text></view>
      <view v-if="loading" class="loading-row" role="status">正在读取当前采购记录…</view>
      <view v-else-if="confirmed.length === 0" class="loading-row">这张单据没有已入账明细。</view>
      <view v-else class="item-list">
        <view v-for="line in confirmed" :key="line.id" class="item-row" :class="{ voided: purchaseFor(line)?.voided_at }">
          <template v-if="purchaseFor(line)">
            <view class="item-name">
              <text class="product">{{ productName(purchaseFor(line)!) }}</text>
              <text v-if="description(purchaseFor(line)!)" class="description">记录描述 · {{ description(purchaseFor(line)!) }}</text>
              <text v-if="purchaseFor(line)!.line_kind === 'adjustment'" class="row-note">调整项</text>
              <text v-if="purchaseFor(line)!.voided_at" class="row-note">这条采购记录已作废</text>
              <text v-else-if="purchaseFor(line)!.amount_mismatch" class="row-note">已确认按行金额入账</text>
            </view>
            <text class="item-quantity">{{ quantity(purchaseFor(line)!) }}</text>
            <text class="item-price">{{ money(purchaseFor(line)!.unit_price, purchaseFor(line)!.currency) }}<text v-if="purchaseFor(line)!.purchase_unit" class="per-unit"> / {{ purchaseFor(line)!.purchase_unit }}</text></text>
            <text class="item-amount">{{ money(purchaseFor(line)!.line_amount, purchaseFor(line)!.currency) }}</text>
          </template>
          <template v-else>
            <view class="item-name"><text class="product">{{ line.description || `第 ${line.line_no} 行` }}</text><text class="row-note">{{ line.purchase_id === null ? '关联采购记录缺失' : '采购记录读取失败' }}</text></view>
            <text class="item-quantity">—</text><text class="item-price">—</text><text class="item-amount">—</text>
          </template>
        </view>
      </view>
      <view v-if="failedIds.length" class="load-error" role="alert">{{ failedIds.length }} 条采购记录未能读取。<button class="retry-button" @click="loadPurchases">重试</button></view>

      <view v-if="skipped.length" class="skipped-section">
        <text class="skipped-title">未入账 · 已跳过 {{ skipped.length }} 行</text>
        <view v-for="line in skipped" :key="line.id" class="skipped-row"><text>{{ line.description || `第 ${line.line_no} 行` }}</text><text>{{ line.skip_reason || '未填写原因' }}</text></view>
      </view>

      <view class="receipt-footer">
        <view><text class="footer-label">完成时入账金额</text><text class="footer-value">{{ money(invoice.reconciliation.confirmed_amount, receiptCurrency) }}</text></view>
        <view v-if="invoice.reconciliation.difference !== null"><text class="footer-label">完成时差额</text><text class="footer-value">{{ money(invoice.reconciliation.difference, receiptCurrency) }}</text></view>
        <text v-if="invoice.completed_at" class="completed-date">完成于 {{ invoice.completed_at.slice(0, 10) }}</text>
      </view>
    </view>

    <view v-if="originalOpen" class="original-overlay" @click.self="originalOpen = false" @keydown.esc="originalOpen = false">
      <view class="original-drawer" role="dialog" aria-modal="true" aria-label="单据原件">
        <view class="drawer-head"><text>单据原件</text><button class="ui-button secondary" @click="originalOpen = false">关闭</button></view>
        <OriginalDocumentViewer :invoice-id="invoice.id" :files="invoice.files" :sources="invoice.intake_sources" :is-fake="invoice.recognition.is_fake" :focus="null" :visible="true" />
      </view>
    </view>
  </view>
</template>

<style scoped>
.receipt-page { max-width: 1160px; width: 100%; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; }
.back-link { align-self: flex-start; min-height: 44px; margin: 0; padding: 7px 0; border: 0; background: transparent; color: var(--green); text-align: left; font-size: 16px; cursor: pointer; }
.back-link::after, .original-button::after, .retry-button::after { border: 0; }
.receipt { overflow: hidden; box-shadow: 0 12px 32px rgba(27, 57, 40, .05); }
.receipt-header { display: flex; justify-content: space-between; gap: 32px; padding: 28px 36px 24px; }
.supplier-block, .total-block { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.supplier-block { flex: 1; }
.eyebrow { margin-bottom: 12px; }
.field-label { color: var(--muted); font-size: 13px; }
.supplier { font-size: clamp(23px, 2.4vw, 29px); font-weight: 750; line-height: 1.25; overflow-wrap: anywhere; }
.metadata { display: flex; flex-wrap: wrap; gap: 8px 22px; margin-top: 10px; color: var(--muted); font-size: 15px; }
.total-block { align-items: flex-end; text-align: right; }
.badges { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px; margin-bottom: 13px; }
.status, .demo { padding: 5px 10px; border-radius: 99px; background: #e7f3eb; color: var(--green); font-size: 13px; font-weight: 700; }
.demo { background: #fff2d8; color: #7b4a03; }
.edit-receipt { width: auto; min-height: 40px; margin: -6px -8px 0 8px; padding: 6px 8px; border: 0; background: transparent; color: var(--green); font-size: 16px; font-weight: 700; cursor: pointer; }
.edit-receipt::after { border: 0; }
.reopen-error { margin: 0; text-align: left; }
.grand-total { font-size: clamp(30px, 3.5vw, 43px); font-weight: 750; line-height: 1.15; white-space: nowrap; }
.currency-edit { min-height: 44px; margin: 0 -8px 0 0; padding: 8px; border: 0; background: transparent; color: var(--green); font-size: 14px; cursor: pointer; }
.currency-edit::after { border: 0; }
.currency-form { display: flex; flex-direction: column; align-items: flex-end; gap: 9px; max-width: 300px; color: var(--muted); font-size: 13px; line-height: 1.5; }
.currency-picker { min-width: 116px; text-align: left; color: var(--ink); }
.currency-actions { display: flex; gap: 8px; }
.currency-form .load-error { margin: 0; }
.summary-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 10px 28px; min-height: 60px; padding: 10px 36px; background: #edf5ed; color: var(--ink); font-size: 15px; }
.summary-bar strong { font-size: 18px; }
.original-button { margin: 0 0 0 auto; padding: 8px 0; border: 0; background: transparent; color: var(--green); font-size: 16px; font-weight: 700; cursor: pointer; }
.import-files { display: flex; flex-wrap: wrap; gap: 8px; padding: 20px 36px 0; }
.import-files .ui-button { max-width: 100%; overflow-wrap: anywhere; }
.reconciliation-alert, .revision-note, .load-error { margin: 20px 36px 0; padding: 11px 14px; border-radius: 7px; background: #fff4df; color: #704007; font-size: 14px; line-height: 1.5; }
.duplicate-note { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px; margin: 20px 36px 0; padding: 11px 14px; border-radius: 7px; background: #fff4df; color: #704007; font-size: 14px; }
.revision-note { background: #edf5ed; color: var(--muted); }
.items-heading { display: flex; justify-content: space-between; align-items: baseline; padding: 30px 36px 14px; font-size: 21px; font-weight: 700; }
.item-count { font-size: 15px; color: var(--muted); font-weight: 400; }
.table-head, .item-row { display: grid; grid-template-columns: minmax(0, 2.5fr) minmax(120px, 1fr) minmax(145px, 1.1fr) minmax(150px, 1.15fr); column-gap: 18px; align-items: center; }
.table-head { margin: 0 36px; padding: 13px 14px; border-top: 2px solid var(--ink); border-bottom: 1px solid var(--line); color: var(--muted); font-size: 15px; font-weight: 650; }
.table-head text:last-child { text-align: right; }
.item-list { padding: 0 36px; }
.item-row { min-height: 90px; padding: 17px 14px; border-bottom: 1px solid var(--line); }
.item-row.voided { background: #f9f5f2; }
.item-name { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.product { font-size: 20px; font-weight: 650; line-height: 1.3; overflow-wrap: anywhere; }
.description { color: var(--muted); font-size: 15px; line-height: 1.4; overflow-wrap: anywhere; }
.row-note { color: var(--warning); font-size: 13px; }
.item-quantity, .item-price, .item-amount { font-size: 18px; line-height: 1.35; overflow-wrap: anywhere; }
.item-amount { text-align: right; font-size: 21px; font-weight: 700; }
.per-unit { color: var(--muted); font-size: 14px; }
.loading-row { padding: 30px 50px; color: var(--muted); }
.retry-button { margin: 0 0 0 7px; padding: 2px 4px; border: 0; background: transparent; color: var(--green); font-weight: 700; cursor: pointer; }
.skipped-section { margin: 28px 36px 0; padding: 20px 14px 6px; border-top: 1px solid var(--line); }
.skipped-title { display: block; margin-bottom: 8px; color: var(--muted); font-size: 16px; font-weight: 700; }
.skipped-row { display: flex; justify-content: space-between; gap: 20px; padding: 10px 0; color: var(--muted); font-size: 15px; }
.skipped-row text:first-child { color: var(--ink); }
.receipt-footer { display: flex; align-items: flex-end; flex-wrap: wrap; gap: 20px 48px; margin: 30px 36px 0; padding: 22px 14px 28px; border-top: 2px solid var(--ink); }
.receipt-footer > view { display: flex; flex-direction: column; gap: 7px; }
.footer-label { color: var(--muted); font-size: 14px; }
.footer-value { font-size: 23px; font-weight: 700; }
.completed-date { margin-left: auto; color: var(--muted); font-size: 14px; }
.original-overlay { position: fixed; z-index: 200; inset: 0; display: flex; justify-content: flex-end; background: rgba(16, 31, 22, .5); }
.original-drawer { display: flex; flex-direction: column; width: min(740px, 100vw); height: 100%; padding: 18px; background: var(--surface); box-shadow: -10px 0 30px rgba(0, 0, 0, .12); }
.drawer-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex: none; margin-bottom: 12px; font-size: 20px; font-weight: 700; }
.original-drawer :deep(.viewer) { min-height: 0; flex: 1; }
.original-drawer :deep(.canvas) { min-height: 0; }
@media (max-width: 700px) {
  .receipt-header { flex-direction: column; gap: 20px; padding: 22px 20px; }
  .total-block { align-items: flex-start; text-align: left; }
  .currency-form { align-items: flex-start; }
  .badges { justify-content: flex-start; margin: 0 0 4px; }
  .summary-bar { padding: 12px 20px; gap: 8px 17px; }
  .original-button { margin-left: 0; }
  .items-heading { padding: 24px 20px 12px; }
  .table-head { display: none; }
  .item-list { padding: 0 20px; }
  .item-row { grid-template-columns: minmax(0, 1fr) auto; gap: 8px 16px; min-height: 0; padding: 17px 0; }
  .item-name { grid-column: 1 / -1; grid-row: 1; }
  .item-quantity { grid-column: 1; grid-row: 2; color: var(--muted); font-size: 16px; }
  .item-amount { grid-column: 2; grid-row: 2; align-self: start; font-size: 20px; white-space: nowrap; }
  .item-price { grid-column: 1 / -1; grid-row: 3; color: var(--muted); font-size: 16px; }
  .product { font-size: 19px; }
  .reconciliation-alert, .revision-note, .load-error, .duplicate-note { margin: 16px 20px 0; }
  .import-files { padding: 16px 20px 0; }
  .skipped-section { margin: 24px 20px 0; }
  .receipt-footer { margin: 24px 20px 0; padding: 20px 0 24px; gap: 20px 32px; }
  .completed-date { margin-left: 0; width: 100%; }
  .original-drawer { padding: 12px; }
}
</style>
