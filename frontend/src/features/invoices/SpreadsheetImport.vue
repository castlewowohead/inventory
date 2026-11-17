<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import {
  ApiError, errorMessage, getPurchaseExport,
  type ImportOptions, type ImportPreview, type Invoice
} from '../../shared/api'
import { formatBytes } from '../../shared/format'
import { downloadAttachment } from '../../shared/platform/download'
import {
  isCancelledPick, pickSpreadsheet, type PickedFile
} from '../../shared/platform/filePicker'
import { confirmLeave, registerLeaveGuard } from '../../shared/platform/leaveGuard'
import { uploadOne, type UploadHandle } from '../../shared/platform/upload'

const emit = defineEmits<{ created: [id: number]; openExisting: [id: number]; close: [] }>()
const file = ref<PickedFile | null>(null)
const preview = ref<ImportPreview | null>(null)
const previewValid = ref(false)
const state = ref<'idle' | 'previewing' | 'creating'>('idle')
const failure = ref('')
const duplicateId = ref<number | null>(null)
const progress = ref(0)
const sheet = ref('')
const availableSheets = ref<string[]>([])
const headerRow = ref('1')
const encoding = ref('utf-8-sig')
const delimiter = ref(',')
const numberFormat = ref('strict')
const dateOrder = ref('strict')
const sourceName = ref('')
const currency = ref('USD')
const mapping = ref<Record<string, string>>({})
const mappingConfirmed = ref(false)
const manualReview = ref(false)
const exportFormat = ref<'csv' | 'xlsx'>('xlsx')
const exporting = ref(false)
const exportError = ref('')
let active: UploadHandle<unknown> | null = null

const fields = [
  ['description', '商品描述'], ['quantity', '数量'], ['purchase_unit', '单位'],
  ['unit_price', '单价'], ['line_amount', '金额'], ['purchase_date', '采购日期'],
  ['source_name', '采购来源'], ['currency', '币种']
] as const
const isCsv = computed(() => /\.csv$/i.test(file.value?.name ?? ''))
const issueCount = computed(() => preview.value?.rows.reduce((n, row) => n + row.review_issues.length, 0) ?? 0)
const canCreate = computed(() => !!file.value && !!preview.value && previewValid.value && state.value === 'idle'
  && mappingConfirmed.value && (issueCount.value === 0 || manualReview.value))

function invalidate() {
  previewValid.value = false
  mappingConfirmed.value = false
  manualReview.value = false
  failure.value = ''
  duplicateId.value = null
}

function choose() {
  pickSpreadsheet().then((files) => {
    if (!files.length) return
    const selected = files[0]
    if (!/\.(csv|xlsx)$/i.test(selected.name)) {
      failure.value = '请选择 .csv 或 .xlsx 文件；旧版 .xls 不受支持。'
      return
    }
    file.value = selected
    preview.value = null
    sheet.value = ''
    availableSheets.value = []
    mapping.value = {}
    invalidate()
  }).catch((reason) => { if (!isCancelledPick(reason)) failure.value = errorMessage(reason) })
}

function options(): ImportOptions {
  const columns: Record<string, number | null> = {}
  for (const [field, value] of Object.entries(mapping.value)) {
    if (value === '0') columns[field] = null
    else if (value.trim()) {
      const parsed = Number(value)
      if (!Number.isSafeInteger(parsed) || parsed < 1) throw new Error(`${field} 的列号须为正整数；0 表示忽略。`)
      columns[field] = parsed
    }
  }
  // 当前界面只录采购行；显式忽略表格中的类型列，避免自动映射出调整行。
  columns.line_kind = null
  const row = Number(headerRow.value)
  if (!Number.isSafeInteger(row) || row < 1 || row > 5000) throw new Error('表头行号须为 1 至 5000。')
  return {
    ...(!isCsv.value && sheet.value ? { sheet: sheet.value } : {}), header_row: row, mapping: columns,
    encoding: encoding.value, delimiter: delimiter.value,
    number_format: numberFormat.value, date_order: dateOrder.value,
    defaults: {
      ...(sourceName.value.trim() ? { source_name: sourceName.value.trim() } : {}),
      ...(currency.value ? { currency: currency.value } : {})
    }
  }
}

function send<T>(path: '/api/imports/preview' | '/api/imports', selected: PickedFile, payload: ImportOptions) {
  const handle = uploadOne<T>(path, selected, {
    fieldName: 'file', formData: { options: JSON.stringify(payload) },
    onProgress: (value) => { progress.value = value.progress }
  })
  active = handle
  return handle.response
}

async function runPreview() {
  if (!file.value || state.value !== 'idle') return
  const selected = file.value
  invalidate()
  state.value = 'previewing'
  progress.value = 0
  try {
    const result = await send<ImportPreview>('/api/imports/preview', selected, options())
    preview.value = result
    previewValid.value = true
    availableSheets.value = result.sheets
    if (!sheet.value && !isCsv.value) sheet.value = result.import_metadata.sheet
    mapping.value = Object.fromEntries(Object.entries(result.import_metadata.mapping)
      .filter(([key]) => key !== 'line_kind')
      .map(([key, value]) => [key, String(value)]))
  } catch (error) {
    failure.value = errorMessage(error)
    if (error instanceof ApiError && Array.isArray(error.details?.sheets)) {
      availableSheets.value = error.details.sheets.filter((name): name is string => typeof name === 'string')
    }
  }
  finally { state.value = 'idle'; active = null }
}

async function create() {
  if (!canCreate.value || !file.value) return
  state.value = 'creating'
  failure.value = ''
  duplicateId.value = null
  progress.value = 0
  let createdId: number | null = null
  try {
    const result = await send<Invoice>('/api/imports', file.value, options())
    file.value = null
    preview.value = null
    previewValid.value = false
    createdId = result.id
  } catch (error) {
    failure.value = errorMessage(error)
    if (error instanceof ApiError && error.status === 409) {
      const id = error.details?.invoice_id
      if (typeof id === 'number') duplicateId.value = id
    }
  } finally { state.value = 'idle'; active = null }
  if (createdId !== null) emit('created', createdId)
}

async function download() {
  exporting.value = true
  exportError.value = ''
  try {
    const response = await getPurchaseExport(exportFormat.value)
    downloadAttachment(response, `purchase-details.${exportFormat.value}`)
  } catch (error) { exportError.value = errorMessage(error) }
  finally { exporting.value = false }
}

async function close() {
  if (await confirmLeave()) emit('close')
}

function openExisting() {
  if (duplicateId.value === null) return
  const id = duplicateId.value
  file.value = null
  preview.value = null
  emit('openExisting', id)
}

function headerFor(field: string): string {
  const fields = preview.value?.rows[0]?.raw_fields.fields
  if (!fields || typeof fields !== 'object') return ''
  const entry = (fields as Record<string, unknown>)[field]
  if (!entry || typeof entry !== 'object') return ''
  const header = (entry as Record<string, unknown>).header
  return typeof header === 'string' ? header : ''
}

const stopGuard = registerLeaveGuard(
  () => state.value !== 'idle' ? '表格正在传送。离开会中断这次请求。'
    : file.value ? '已选表格还未创建待审核单据，离开会丢失预览。' : null,
  () => { active?.abort(); file.value = null; preview.value = null }
)
onUnmounted(() => { stopGuard(); active?.abort() })
</script>

<template>
  <view class="sheet-panel ui-card">
    <view class="heading"><view><text class="title">表格导入</text><text class="muted">先预览，再创建待审核单据；预览不会保存数据。</text></view><button class="ui-button secondary" :disabled="state !== 'idle'" @click="close">收起</button></view>
    <view class="actions"><button class="ui-button secondary" :disabled="state !== 'idle'" @click="choose">选择 CSV / XLSX</button><text v-if="file">{{ file.name }} · {{ formatBytes(file.size) }}</text></view>
    <template v-if="file">
      <view class="options">
        <label v-if="!isCsv">工作表<input v-model="sheet" placeholder="留空用首张；也可填名称" @input="invalidate" /><text v-if="availableSheets.length" class="muted">可选：{{ availableSheets.join('、') }}</text></label>
        <label>表头原始行号<input v-model="headerRow" type="number" min="1" max="5000" @input="invalidate" /></label>
        <label>编码<select v-model="encoding" @change="invalidate"><option value="utf-8-sig">UTF-8（含 BOM）</option><option value="utf-8">UTF-8</option><option value="gb18030">GB18030</option><option value="utf-16">UTF-16</option></select></label>
        <label>分隔符<select v-model="delimiter" @change="invalidate"><option value=",">逗号</option><option value=";">分号</option><option value="&#9;">Tab</option></select></label>
        <label>数字格式<select v-model="numberFormat" @change="invalidate"><option value="strict">严格：歧义留待处理</option><option value="decimal_dot">小数点 .</option><option value="decimal_comma">小数逗号 ,</option></select></label>
        <label>日期顺序<select v-model="dateOrder" @change="invalidate"><option value="strict">严格：歧义留待处理</option><option value="mdy">月 / 日 / 年</option><option value="dmy">日 / 月 / 年</option></select></label>
        <label>整批采购来源<input v-model="sourceName" placeholder="可选" @input="invalidate" /></label>
        <label>整批币种（默认美元，可修改）<select v-model="currency" @change="invalidate"><option value="USD">USD</option><option value="CNY">CNY</option></select></label>
      </view>
      <view class="mapping"><text class="section-title">列映射 · 表头第 {{ headerRow }} 行</text><text class="muted">留空自动匹配；列号从 1 开始，0 忽略。遇到重复列名可手动填列号；修改后须重新预览。</text><view class="mapping-grid"><label v-for="[key, label] in fields" :key="key">{{ label }}<text v-if="headerFor(key)" class="muted">表头：{{ headerFor(key) }}</text><input v-model="mapping[key]" type="number" min="0" placeholder="自动" @input="invalidate" /></label></view></view>
      <view class="actions"><button class="ui-button secondary" :disabled="state !== 'idle'" @click="runPreview">{{ preview ? '重新预览' : '预览表格' }}</button><text v-if="state !== 'idle'" role="status">{{ state === 'previewing' ? '预览传送中' : '创建批次中' }} · {{ progress }}%</text></view>
      <view v-if="preview" class="preview"><text class="section-title">只读预览 · {{ preview.import_metadata.sheet }} · {{ preview.import_metadata.row_count }} 行</text><text v-if="!previewValid" class="issue">设置已变化，下列是上次预览结果。请重新预览。</text><text class="muted">空白行忽略 {{ preview.import_metadata.blank_rows_ignored }} 行；每行确认前仍需核对。</text><view class="row" v-for="row in preview.rows" :key="row.source_row"><text class="row-title">原始第 {{ row.source_row }} 行 · {{ row.values.description || '描述待填' }}</text><text>数量 {{ row.values.quantity ?? '待填' }} · 单位 {{ row.values.purchase_unit ?? '待填' }} · 单价 {{ row.values.unit_price ?? '待填' }} · 金额 {{ row.values.line_amount ?? '待填' }} · 日期 {{ row.values.purchase_date ?? '待填' }}</text><text v-for="(issue, index) in row.review_issues" :key="index" class="issue">{{ issue.cell || `第 ${row.source_row} 行` }} · {{ issue.field }}：{{ issue.message }}</text></view><text v-if="issueCount" class="issue">共 {{ issueCount }} 处待处理。请选正确数字/日期规则后重预览，或创建后逐行手工修正。</text><button class="choice" :aria-pressed="mappingConfirmed" @click="mappingConfirmed = !mappingConfirmed">{{ mappingConfirmed ? '☑' : '□' }} 我已核对列映射、数字与日期格式</button><button v-if="issueCount" class="choice" :aria-pressed="manualReview" @click="manualReview = !manualReview">{{ manualReview ? '☑' : '□' }} 剩余异常将在审核中逐行手工处理</button><text class="muted">{{ preview.import_metadata.notice }}</text><button class="ui-button" :disabled="!canCreate" @click="create">创建待审核单据</button></view>
    </template>
    <view v-if="failure" role="alert" class="error">{{ failure }} <button v-if="duplicateId" class="link" @click="openExisting">打开原批次 #{{ duplicateId }}</button></view>
    <view class="export"><text class="section-title">采购明细导出</text><text class="muted">仅含已入账采购明细，不含已作废记录。导出全量结果，不使用分页参数。</text><view class="actions"><select v-model="exportFormat" aria-label="导出格式"><option value="xlsx">XLSX</option><option value="csv">CSV</option></select><button class="ui-button secondary" :disabled="exporting" @click="download">{{ exporting ? '正在下载…' : '下载明细' }}</button></view><text v-if="exportError" role="alert" class="error">{{ exportError }}</text></view>
  </view>
</template>

<style scoped>
.sheet-panel { display: flex; flex-direction: column; gap: 20px; padding: 24px; }
.heading,.actions { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.heading > view { display: flex; flex-direction: column; gap: 4px; }
.title { font-size: 21px; font-weight: 700; }.section-title { display: block; font-size: 16px; font-weight: 700; }.muted { display: block; color: var(--muted); font-size: 13px; }
.options,.mapping-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(185px,1fr)); gap: 12px; }.options label,.mapping-grid label { display: flex; flex-direction: column; gap: 5px; font-size: 13px; }
input,select { box-sizing: border-box; width: 100%; min-height: 44px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 5px; background: #fff; color: var(--ink); font: inherit; }
.mapping,.preview,.export { display: flex; flex-direction: column; gap: 10px; }.row { display: flex; flex-direction: column; gap: 4px; padding: 10px 0; border-top: 1px solid var(--line); font-size: 13px; }.row-title { font-weight: 700; }.issue,.error { color: var(--error); font-size: 13px; }.choice { min-height: 44px; margin: 0; padding: 8px; border: 1px solid var(--line); background: #fff; color: var(--ink); text-align: left; font-size: 13px; cursor: pointer; }.choice::after,.link::after { border: 0; }.link { min-height: 44px; border: 0; background: transparent; color: var(--green); text-decoration: underline; }.export { padding-top: 18px; border-top: 1px solid var(--line); }
@media(max-width:899px) { .sheet-panel { padding: 16px; } .options,.mapping-grid { grid-template-columns: 1fr; } }
</style>
