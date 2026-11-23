<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { ApiError, errorMessage, getInvoice, reviseIntakeReviewClassification, updateInvoice } from '../../shared/api'
import type { Invoice, InvoiceHeaderInput } from '../../shared/api'
import { registerLeaveGuard } from '../../shared/platform/leaveGuard'
import { readDocumentEvidence } from './evidence'
import { plainAmount } from './lineDraft'

type HeaderKey = keyof InvoiceHeaderInput
const fields: { key: HeaderKey; label: string }[] = [
  { key: 'source_name', label: '来源' },
  { key: 'invoice_date', label: '日期 *' },
  { key: 'invoice_number', label: '单号' },
  { key: 'currency', label: '币种' },
  { key: 'invoice_total', label: '总额' }
]
const evidenceKey: Record<HeaderKey, string> = {
  source_name: 'supplier', invoice_date: 'invoice_date', invoice_number: 'invoice_number',
  currency: 'currency', invoice_total: 'invoice_total'
}
type HeaderForm = Record<HeaderKey, string>
const props = defineProps<{ invoice: Invoice; canManage: boolean }>()
const emit = defineEmits<{ stored: [invoice: Invoice] }>()
const form = ref<HeaderForm>({ source_name: '', invoice_date: '', invoice_number: '', currency: 'USD', invoice_total: '' })
const baseline = ref<HeaderForm>({ ...form.value })
const editingSuggestions = ref<Set<HeaderKey>>(new Set())
const saving = ref(false)
let saveTask: Promise<boolean> | null = null
const errorText = ref('')
const fieldError = ref<Record<string, string>>({})
const evidence = computed(() => readDocumentEvidence(props.invoice.recognition.raw_document_fields))
const sourceEvidence = computed(() => evidence.value?.fields.filter((item) =>
  ['supplier', 'supplier_english', 'supplier_chinese', 'supplier_evidence'].includes(item.key)
) ?? [])

function values(item: Invoice): HeaderForm {
  return {
    source_name: item.source_name ?? '', invoice_date: item.invoice_date ?? '',
    invoice_number: item.invoice_number ?? '', currency: item.currency ?? 'USD',
    invoice_total: item.invoice_total ?? ''
  }
}
function reset(item: Invoice) {
  form.value = values(item)
  baseline.value = values(item)
  editingSuggestions.value = new Set()
  fieldError.value = {}
  errorText.value = ''
}
const dirty = computed(() => fields.some(({ key }) => form.value[key] !== baseline.value[key]) || editingSuggestions.value.size > 0)
watch(() => props.invoice.id, () => reset(props.invoice), { immediate: true })
watch(() => props.invoice, (item) => { if (!dirty.value) reset(item) })

function suggestion(key: HeaderKey): string | null {
  if (!props.canManage || props.invoice.header_reviewed_fields.includes(key) || editingSuggestions.value.has(key)) return null
  const field = evidence.value?.fields.find((item) => item.key === evidenceKey[key])
  if (field?.status !== 'recognized') return null
  return values(props.invoice)[key] || field.rawText || null
}

async function save(keys?: HeaderKey[]): Promise<boolean> {
  if (!props.canManage) return false
  if (saveTask) {
    if (!await saveTask) return false
    return save(keys)
  }
  const task = saveOnce(keys)
  saveTask = task
  try { return await task } finally { if (saveTask === task) saveTask = null }
}

async function saveOnce(keys?: HeaderKey[]): Promise<boolean> {
  errorText.value = ''
  fieldError.value = {}
  const changed = (keys ?? fields.map(({ key }) => key)).filter((key) =>
    form.value[key] !== baseline.value[key] || editingSuggestions.value.has(key))
  if (!changed.length) return true
  const submitted = { ...form.value }
  const patch: Partial<InvoiceHeaderInput> = {}
  for (const key of changed) patch[key] = key === 'currency' ? form.value[key] || 'USD' : form.value[key].trim() || null
  if (patch.invoice_total !== undefined && patch.invoice_total !== null && !/^\d+(?:\.\d{1,2})?$/.test(patch.invoice_total)) {
    fieldError.value = { invoice_total: '请输入非负金额，最多两位小数。' }
    return false
  }
  const classificationChanged = props.invoice.intake_sources.length > 0 &&
    (patch.invoice_date !== undefined || patch.source_name !== undefined)
  if (classificationChanged && (!form.value.invoice_date.trim() || !form.value.source_name.trim())) {
    fieldError.value = { invoice_date: '批次送审草稿必须保留已确认的日期和来源。' }
    return false
  }
  saving.value = true
  try {
    let stored: Invoice
    if (classificationChanged) {
      const sources = props.invoice.intake_sources
      await reviseIntakeReviewClassification(sources[0]!.batch_id, props.invoice.id,
        sources.map((source) => ({ id: source.item_id, version: source.version })),
        form.value.invoice_date.trim(), form.value.source_name.trim())
      delete patch.invoice_date
      delete patch.source_name
      stored = Object.keys(patch).length ? await updateInvoice(props.invoice.id, patch) : await getInvoice(props.invoice.id)
    } else {
      stored = await updateInvoice(props.invoice.id, patch)
    }
    const next = values(stored)
    const current = { ...form.value }
    for (const key of changed) {
      if (current[key] === submitted[key]) current[key] = next[key]
    }
    form.value = current
    baseline.value = { ...baseline.value, ...Object.fromEntries(changed.map((key) => [key, next[key]])) }
    editingSuggestions.value = new Set([...editingSuggestions.value].filter((key) => !changed.includes(key)))
    emit('stored', stored)
    return true
  } catch (error) {
    errorText.value = errorMessage(error)
    if (error instanceof ApiError && typeof error.details?.field === 'string') {
      fieldError.value = { [error.details.field]: error.message }
    }
    return false
  } finally { saving.value = false }
}

async function savePending(): Promise<boolean> {
  while (saveTask || dirty.value) {
    if (!await save()) return false
  }
  return true
}

function accept(key: HeaderKey) {
  const value = suggestion(key)
  if (!value) return
  // 纸上的总额印的是「$1,040.00」，这个框只收 `1040.00`。照单收下会存不进去，
  // 而失败只会在行那边显示成「请先检查并保存单据信息」（多文件计划 M12）。
  form.value[key] = key === 'invoice_total' ? plainAmount(value) : value
  editingSuggestions.value = new Set([...editingSuggestions.value, key])
  void save([key])
}
function edit(key: HeaderKey) {
  form.value[key] = ''
  editingSuggestions.value = new Set([...editingSuggestions.value, key])
}
function changeCurrency(value: string | number) {
  form.value.currency = ['USD', 'CNY'][Number(value)] ?? 'USD'
  void save(['currency'])
}
function calculateTotal() {
  const summary = props.invoice.reconciliation
  if (!summary.confirmed_count) { errorText.value = '请先确认采购明细，再计算总额。'; return }
  if (summary.confirmed_amount === null) { errorText.value = '已确认的明细使用不同币种，无法直接相加。'; return }
  if (summary.status === 'currency_mismatch' || summary.status === 'currency_required') {
    errorText.value = '请先统一单据和采购明细的币种。'
    return
  }
  form.value.invoice_total = summary.confirmed_amount
  editingSuggestions.value = new Set([...editingSuggestions.value, 'invoice_total'])
  errorText.value = ''
  void save(['invoice_total'])
}
const stopGuard = registerLeaveGuard(
  () => dirty.value ? '单据信息有未保存的修改。离开会丢掉这些修改。' : null,
  () => reset(props.invoice)
)
defineExpose({ hasUnsavedChanges: () => dirty.value || saving.value, savePending })
onUnmounted(stopGuard)
</script>

<template>
  <view class="ui-card header-card" role="region" aria-label="单据信息">
    <text class="section-title">单据信息</text>
    <text v-if="invoice.recognition.is_fake" class="muted">演示识别结果，请对照原件核对。</text>
    <text v-if="invoice.intake_sources.length" class="muted">修改日期或来源会同步这张草稿关联的 {{ invoice.intake_sources.length }} 个整理内容，以及未设置行级覆盖的已入账采购记录。</text>
    <view class="header-grid">
      <view v-for="field in fields" :key="field.key" class="field">
        <view class="field-heading"><text class="label">{{ field.label }}</text><text v-if="suggestion(field.key)" class="ai-badge">AI 识别 · 待核对</text></view>
        <template v-if="canManage">
          <view v-if="field.key === 'currency' && !suggestion(field.key)">
            <picker :range="['USD', 'CNY']" :value="['USD', 'CNY'].indexOf(form.currency)" @change="changeCurrency($event.detail.value)"><view class="ui-input picker-text">{{ form.currency }}</view></picker>
          </view>
          <input v-else-if="suggestion(field.key)" class="ui-input suggested" :value="''" :placeholder="suggestion(field.key) || ''" :aria-label="field.label" readonly />
          <input v-else v-model="form[field.key]" class="ui-input" :inputmode="field.key === 'invoice_total' ? 'decimal' : 'text'" :aria-label="field.label" :placeholder="field.key === 'invoice_date' ? 'YYYY-MM-DD' : '待填'" @blur="save([field.key])" />
          <view v-if="suggestion(field.key)" class="ai-review"><text>请对照原件核对</text><view class="ai-actions"><button class="accept" :disabled="saving" @click="accept(field.key)">此数据正确</button><button class="edit" :disabled="saving" @click="edit(field.key)">修改</button></view></view>
          <view v-if="field.key === 'source_name' && sourceEvidence.length" class="source-evidence">
            <text>以下是识别线索，请对照原件确认。</text>
            <text v-for="item in sourceEvidence" :key="item.key">
              {{ item.label }}：{{ item.rawText ?? (item.status === 'missing' ? '模型判断纸面未见' : '待核对') }}<template v-if="item.page">（第 {{ item.page }} 页）</template>
              <template v-if="item.modelValues.length > 1 && !item.agreement?.includes('多模型一致')">；模型读法：{{ item.modelValues.join(' / ') }}</template>
              <template v-if="item.agreement && !item.agreement.includes('多模型一致')">；{{ item.agreement }}</template>
            </text>
          </view>
          <button v-if="field.key === 'invoice_total'" class="calculate" :disabled="saving" @click="calculateTotal">计算总额</button>
          <text v-if="fieldError[field.key]" class="field-error" role="alert">{{ fieldError[field.key] }}</text>
        </template>
        <text v-else>{{ form[field.key] || '待填' }}</text>
      </view>
    </view>
    <text v-if="errorText" class="field-error" role="alert">{{ errorText }}</text>
    <view v-if="dirty && canManage" class="form-actions"><button class="ui-button" :disabled="saving" @click="save()">{{ saving ? '保存中…' : '保存单据信息' }}</button><button class="ui-button secondary" :disabled="saving" @click="reset(invoice)">取消修改</button></view>
  </view>
</template>

<style scoped>
.header-card { display:flex; flex-direction:column; gap:16px; padding:22px; min-width:0; }
.section-title { font-size:18px; font-weight:650; }
.header-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px 24px; }
.field { display:flex; flex-direction:column; gap:7px; min-width:0; }
.field-heading { display:flex; justify-content:space-between; gap:8px; }
.label, .muted { color:var(--muted); font-size:13px; }
.ai-badge { color:var(--muted); font-size:12px; }
.suggested::placeholder { color:#8b9790; opacity:1; }
.ai-review { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; color:var(--muted); font-size:13px; }
.source-evidence { display:flex; flex-direction:column; gap:3px; color:var(--muted); font-size:12px; overflow-wrap:anywhere; }
.ai-actions, .form-actions { display:flex; gap:8px; flex-wrap:wrap; }
.ai-actions button { width:auto; min-height:42px; margin:0; padding:8px 12px; color:#fff; border:0; font-size:13px; }
.ai-actions button::after, .calculate::after { border:0; }
.accept { background:var(--green); }
.edit { background:#a23f32; }
.calculate { align-self:flex-start; width:auto; min-height:38px; margin:0; padding:6px 0; border:0; background:transparent; color:var(--green); font-size:14px; }
.picker-text { display:flex; align-items:center; }
.field-error { color:var(--error); font-size:13px; }
@media (max-width:899px) { .header-card { padding:16px; } .header-grid { grid-template-columns:1fr; gap:14px; } }
</style>
