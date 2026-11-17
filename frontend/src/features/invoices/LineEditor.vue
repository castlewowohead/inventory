<script setup lang="ts">
/**
 * The draft form for one invoice line (F07).
 *
 * This is where the user turns what is printed or written on the paper into
 * stored values. The rules it exists to hold:
 *
 * - **A save carries `version`.** `_check_version` skips the check when the
 *   field is absent, so leaving it out would turn every save into a blind
 *   overwrite of whatever someone else stored meanwhile. A 409 re-reads the
 *   line and shows the two values side by side; this screen never picks one.
 * - **Only edited fields are sent.** The backend reads an absent key as "keep"
 *   and an explicit `null` as "set to unknown", so a full-form PATCH would push
 *   stale values over fresh ones even when the version still matched.
 * - **An empty cell stays empty.** The extraction leaves what it could not read
 *   as `null` on purpose, and `0` means a real thing here (a free line). The
 *   form shows which of the two each value is, and never converts between them.
 * - **`missing_fields` and `review_issues` are the server's words.** An issue
 *   blocks confirmation until the field it names is PATCHed — the backend drops
 *   it only then — so the form says which field clears which issue.
 *
 * Completing the invoice is F09. An already
 * confirmed line is read-only here, because the backend refuses to PATCH it:
 * its numbers are in the purchase history, and editing the draft in place would
 * leave the two telling different stories.
 */
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import {
  addInvoiceLine, ApiError, confirmInvoiceLine, deleteInvoiceLine, errorMessage,
  getFoodItems, getInvoice, unconfirmInvoiceLine, updateInvoiceLine,
  type Invoice, type InvoiceLine
} from '../../shared/api'
import { registerLeaveGuard } from '../../shared/platform/leaveGuard'
import FoodItemMatcher from './FoodItemMatcher.vue'
import { suggestedValue } from './evidence'
import {
  changedFields, draftOf, FIELD_LABEL, ISSUE_FIELDS, issuesByField, missingLabel,
  patchBody, validateDraft,
  type DraftField, type LineDraft
} from './lineDraft'

const props = defineProps<{ invoice: Invoice; line: InvoiceLine }>()

const emit = defineEmits<{
  /** The server's new version of this line; the parent replaces its copy. */
  applied: [line: InvoiceLine]
  stored: [invoice: Invoice, advance: boolean]
  /** Re-read the whole invoice (line added or deleted changes the counts). */
  reload: []
}>()

const CURRENCIES = ['', 'USD', 'CNY']
const MONEY_FIELDS = ['quantity', 'unit_price', 'line_amount'] as const

const draft = ref<LineDraft>(draftOf(props.line))
const newCategoryId = ref<number | null>(null)
const fieldErrors = ref<Partial<Record<DraftField, string>>>({})
const saving = ref(false)
const saveError = ref('')
const savedNote = ref('')
const showOverrides = ref(false)
const deleting = ref(false)
const addingLine = ref(false)
const actionBusy = ref(false)
const actionError = ref('')
const actionNote = ref('')
const skipReason = ref('')
const skipError = ref('')
const showSkip = ref(false)
const mismatch = ref<Record<string, unknown> | null>(null)
/** The draft as it stood when a 409 came back, kept for the comparison panel. */
const conflict = ref<{ mine: LineDraft; message: string; versions: string | null } | null>(null)

const readOnly = computed(() => props.line.status === 'confirmed' || props.invoice.status === 'voided' || props.invoice.status === 'completed')
const changed = computed(() => changedFields(draft.value, props.line))
const dirty = computed(() => changed.value.length > 0)
const issues = computed(() => issuesByField(props.line.review_issues ?? []))
/** Issues whose field has no input of its own here; they get their own block so
 *  nothing the backend recorded is dropped on the floor. */
const generalIssues = computed(() => (props.line.review_issues ?? []).filter(
  (issue) => !(typeof issue.field === 'string' && ISSUE_FIELDS.has(issue.field))))
const missing = computed(() => (props.line.missing_fields ?? []).filter(
  (name) => name !== 'food_item_id' || (!props.line.food_item && newCategoryId.value === null)))
/** Hand-added lines have no `raw_fields`; only those may be deleted. */
const handAdded = computed(() => Object.keys(props.line.raw_fields ?? {}).length === 0)
const serverDraft = computed(() => draftOf(props.line))
const dismissed = ref<Set<DraftField>>(new Set())
const activeCandidate = ref<DraftField | null>(null)
const focusField = ref<DraftField | null>(null)
const candidateFields = ['description', 'quantity', 'unit_price', 'line_amount', 'purchase_unit'] as const
const cropField = {
  description: 'item_description', quantity: 'quantity', unit_price: 'unit_price',
  line_amount: 'line_amount', purchase_unit: 'unit'
} as const

function suggestion(name: DraftField): string | null {
  if (!(name in cropField) || draft.value[name].trim() || dismissed.value.has(name) || readOnly.value) return null
  return suggestedValue(props.line, cropField[name as keyof typeof cropField])
}

function acceptSuggestion(name: DraftField) {
  const value = suggestion(name)
  if (!value) return
  draft.value[name] = value
  dismissed.value = new Set([...dismissed.value, name])
  activeCandidate.value = candidateFields.find((key) => suggestion(key)) ?? null
}

async function editSuggestion(name: DraftField) {
  dismissed.value = new Set([...dismissed.value, name])
  draft.value[name] = ''
  activeCandidate.value = candidateFields.find((key) => suggestion(key)) ?? null
  focusField.value = null
  await nextTick()
  focusField.value = name
}

function inheritedNote(name: 'purchase_date' | 'source_name' | 'currency'): string {
  if (draft.value[name].trim()) return '这一行用你填的'
  const header = name === 'purchase_date' ? props.invoice.invoice_date
    : name === 'source_name' ? props.invoice.source_name : props.invoice.currency || 'USD'
  return header ? `不填就按单据上的 ${header} 记` : '单据上这一项也是空的'
}

// Leaving the page with unsaved cell edits asks first (plan section 6).
const stopGuard = registerLeaveGuard(
  () => (dirty.value && !readOnly.value
    ? `第 ${props.line.line_no} 行改了 ${changed.value.map((key) => FIELD_LABEL[key]).join('、')}，还没有保存。离开会丢掉这些修改。`
    : null),
  () => { draft.value = draftOf(props.line) }
)
onUnmounted(stopGuard)

/**
 * Asked by the parent before it moves to another line.
 *
 * Resolves true when it is safe to leave this line. "放弃修改" is the only way
 * past it, so an edit is never dropped without the user saying so.
 */
function confirmLeaveLine(): Promise<boolean> {
  if (!dirty.value || readOnly.value) return Promise.resolve(true)
  const names = changed.value.map((key) => FIELD_LABEL[key]).join('、')
  return new Promise((resolve) => {
    uni.showModal({
      title: '这一行还没保存',
      content: `第 ${props.line.line_no} 行改了 ${names}，还没有保存。先点「保存这一行」，或者放弃这些修改。`,
      confirmText: '放弃修改',
      cancelText: '留在本行',
      success: (result) => {
        if (result.confirm) draft.value = draftOf(props.line)
        resolve(Boolean(result.confirm))
      },
      fail: () => resolve(false)
    })
  })
}

defineExpose({ confirmLeaveLine, hasUnsavedEdits: () => dirty.value && !readOnly.value })

/** One PATCH. `extra` lets the food matcher save `food_item_id` alongside the cells. */
async function patch(extra: Record<string, unknown> = {}, note = '已保存，并已读回服务端的新版本。'): Promise<InvoiceLine | null> {
  if (saving.value || readOnly.value) return null
  savedNote.value = ''
  saveError.value = ''
  const errors = validateDraft(draft.value)
  fieldErrors.value = errors
  if (Object.keys(errors).length) {
    saveError.value = '有字段还不符合格式，请先按字段旁的提示修改。'
    return null
  }
  const body = { ...(patchBody(draft.value, props.line) ?? { version: props.line.version }), ...extra }
  saving.value = true
  try {
    let stored = await updateInvoiceLine(props.invoice.id, props.line.id, body)
    if ('food_item_id' in body) {
      // PATCH can return the old food_item relationship even though the new ID
      // is committed. A fresh invoice GET has the selected item and version.
      try {
        const current = await getInvoice(props.invoice.id)
        const currentLine = current.lines.find((line) => line.id === stored.id)
        if (!currentLine) throw new Error('重新读取的单据没有这一行。')
        stored = currentLine
        emit('stored', current, false)
      } catch (readError) {
        saveError.value = `食材选择已保存，但重新读取失败：${errorMessage(readError)}。请点「重新读取详情」后再确认。`
        return null
      }
    } else {
      emit('applied', stored)
    }
    conflict.value = null
    // The server's stored values win: it may have trimmed or re-formatted them
    // (`12.345` comes back as `12.345` but `10.0000` reads back as `10`), and the
    // form must show what is actually stored, not what was typed.
    draft.value = draftOf(stored)
    savedNote.value = note
    mismatch.value = null
    return stored
  } catch (error) {
    saveError.value = errorMessage(error)
    if (error instanceof ApiError) {
      const field = error.details?.field
      if (typeof field === 'string' && field in FIELD_LABEL) {
        fieldErrors.value = { ...fieldErrors.value, [field as DraftField]: error.message }
      }
      if (error.status === 409) await handleConflict(error)
    }
  } finally { saving.value = false }
  return null
}

function showActionFailure(code: string, message: string, details?: Record<string, unknown>) {
  actionError.value = message
  mismatch.value = code === 'amount_mismatch' ? details ?? {} : null
  const missing = details?.missing_fields
  if (Array.isArray(missing)) {
    fieldErrors.value = Object.fromEntries(missing.filter((name): name is DraftField =>
      typeof name === 'string' && name in FIELD_LABEL).map((name) => [name, '确认前请填写并保存此字段。']))
  }
  if (code === 'conflict') emit('reload')
}

/** Save edited cells first, then confirm with the version returned by PATCH. */
async function confirmLine(acceptMismatch = false) {
  if (actionBusy.value || saving.value || props.line.status !== 'pending' || readOnly.value) return
  actionBusy.value = true
  actionError.value = ''
  actionNote.value = ''
  try {
    const stored = dirty.value ? await patch() : props.line
    if (!stored) return
    let existingFoodId: number | null = null
    const name = stored.description?.trim() ?? ''
    if (!stored.food_item && stored.line_kind === 'purchase' && !name) {
      actionError.value = '请先填写这行的商品名称。'
      return
    }
    if (!stored.food_item && stored.line_kind === 'purchase' && !stored.purchase_unit?.trim()) {
      actionError.value = '请先填写这行的采购单位，首次入账时会作为食材的默认单位。'
      return
    }
    if (!stored.food_item && stored.line_kind === 'purchase' && name && name.length <= 100) {
      try {
        const page = await getFoodItems(`?q=${encodeURIComponent(name)}&limit=20`)
        const normalized = (value: string) => value.normalize('NFKC').trim().replace(/\s+/g, ' ').toLowerCase()
        const exact = page.items.find((item) => normalized(item.name) === normalized(name)
          || (item.name_zh && normalized(item.name_zh) === normalized(name)))
        existingFoodId = exact?.id ?? null
      } catch (error) {
        actionError.value = `查找已有食材失败：${errorMessage(error)}。请重试确认。`
        return
      }
    }
    if (!stored.food_item && existingFoodId === null && stored.line_kind === 'purchase' && newCategoryId.value === null) {
      actionError.value = '食材库没有这个商品。首次入账前请选择分类。'
      return
    }
    const choice = {
      line_id: stored.id, version: stored.version,
      ...(stored.food_item || existingFoodId ? { food_item_id: stored.food_item?.id ?? existingFoodId }
        : stored.line_kind === 'adjustment' ? { food_item_id: null }
          : { new_food_item: {
            name,
            category_id: newCategoryId.value as number,
            use_purchase_unit_as_base: true as const
          } }),
      ...(acceptMismatch ? { accept_amount_mismatch: true } : {})
    }
    const outcome = await confirmInvoiceLine(props.invoice.id, choice)
    const result = outcome.results.find((item) => item.line_id === stored.id)
    if (!result?.ok) {
      emit('stored', outcome.invoice, false)
      showActionFailure(result?.error?.code ?? 'unexpected_result',
        result?.error?.message ?? '确认请求已返回，但没有这一行的成功结果。请重新读取单据。',
        result?.error?.details)
      return
    }
    // Confirming an auto-created food item clears needs_review on the server.
    // Read the invoice again so the marker comes from the persisted food item,
    // not from a local guess or a stale nested reference in the POST response.
    let confirmedInvoice = outcome.invoice
    try {
      confirmedInvoice = await getInvoice(props.invoice.id)
    } catch (readError) {
      actionError.value = `已入账，但重新读取“待确认”状态失败：${errorMessage(readError)}。请点“重新读取详情”核对。`
    }
    emit('stored', confirmedInvoice, true)
    mismatch.value = null
    actionNote.value = `第 ${stored.line_no} 行已入账，采购记录 #${result.purchase_id}。`
  } catch (error) {
    actionError.value = errorMessage(error)
    if (error instanceof ApiError && error.status === 409) {
      actionError.value += ' 请重新核对服务端状态。'
      emit('reload')
    }
  } finally { actionBusy.value = false }
}

/** Skipping is a versioned state change and never creates a purchase. */
async function skipLine() {
  if (actionBusy.value || props.line.status !== 'pending') return
  skipError.value = ''
  if (!skipReason.value.trim()) {
    skipError.value = '请先写明跳过原因。'
    return
  }
  if (!await confirmLeaveLine()) return
  actionBusy.value = true
  actionError.value = ''
  let changedOnServer = false
  try {
    await updateInvoiceLine(props.invoice.id, props.line.id, {
      version: props.line.version, status: 'skipped', skip_reason: skipReason.value.trim()
    })
    changedOnServer = true
    emit('stored', await getInvoice(props.invoice.id), true)
    showSkip.value = false
    actionNote.value = '这一行已跳过；没有生成采购记录。'
  } catch (error) {
    actionError.value = changedOnServer
      ? `跳过请求已成功，但详情回读失败：${errorMessage(error)}。请点「重新读取详情」核对当前状态。`
      : errorMessage(error)
    if (error instanceof ApiError && error.status === 409) emit('reload')
  } finally { actionBusy.value = false }
}

async function restoreLine() {
  if (actionBusy.value || props.line.status !== 'skipped') return
  if (!await confirmLeaveLine()) return
  actionBusy.value = true
  actionError.value = ''
  let changedOnServer = false
  try {
    await updateInvoiceLine(props.invoice.id, props.line.id, {
      version: props.line.version, status: 'pending'
    })
    changedOnServer = true
    emit('stored', await getInvoice(props.invoice.id), false)
    actionNote.value = '这一行已退回待核对，跳过原因已清除。'
  } catch (error) {
    actionError.value = changedOnServer
      ? `恢复请求已成功，但详情回读失败：${errorMessage(error)}。请点「重新读取详情」核对当前状态。`
      : errorMessage(error)
    if (error instanceof ApiError && error.status === 409) emit('reload')
  } finally { actionBusy.value = false }
}

async function unconfirmLine() {
  if (actionBusy.value || props.line.status !== 'confirmed') return
  const ok = await new Promise<boolean>((resolve) => uni.showModal({
    title: '撤销这行的入账？',
    content: `第 ${props.line.line_no} 行的采购记录 #${props.line.purchase_id ?? '未知'} 将作废并保留追溯记录；这一行会退回待核对。`,
    confirmText: '撤销确认', cancelText: '保留入账',
    success: (result) => resolve(Boolean(result.confirm)), fail: () => resolve(false)
  }))
  if (!ok) return
  actionBusy.value = true
  actionError.value = ''
  let changedOnServer = false
  try {
    await unconfirmInvoiceLine(props.invoice.id, props.line.id, props.line.version)
    changedOnServer = true
    emit('stored', await getInvoice(props.invoice.id), false)
    actionNote.value = '采购记录已作废，这一行已退回待核对。'
  } catch (error) {
    actionError.value = changedOnServer
      ? `撤销请求已成功，但详情回读失败：${errorMessage(error)}。请点「重新读取详情」核对当前状态。`
      : errorMessage(error)
    if (error instanceof ApiError && error.status === 409) emit('reload')
  } finally { actionBusy.value = false }
}

/**
 * A 409: someone stored a different version of this line while it was open.
 *
 * The line is re-read and both values are shown. Nothing is merged
 * automatically — the two numbers may both be plausible, and picking one for
 * the user is how the wrong price gets stored without anyone noticing.
 */
async function handleConflict(error: ApiError) {
  const expected = error.details?.expected_version
  const current = error.details?.current_version
  conflict.value = {
    mine: { ...draft.value },
    message: error.message,
    versions: typeof expected === 'number' && typeof current === 'number'
      ? `你打开的是第 ${expected} 版，服务端现在是第 ${current} 版。`
      : null
  }
  // Re-reading is the whole point of the 409: the parent gets the current line
  // so "服务端现在的值" below is the server's, not a guess.
  emit('reload')
}

function resolveWithServer() {
  draft.value = draftOf(props.line)
  fieldErrors.value = {}
  conflict.value = null
  saveError.value = ''
  savedNote.value = '已改用服务端的当前值。你刚才的输入已经丢弃。'
}

function resolveWithMine() {
  draft.value = { ...(conflict.value?.mine ?? draft.value) }
  conflict.value = null
  void patch({}, '已按服务端的新版本重新保存你的输入。')
}

function discard() {
  draft.value = draftOf(props.line)
  fieldErrors.value = {}
  saveError.value = ''
  savedNote.value = ''
}

async function addBlankLine() {
  if (addingLine.value) return
  if (!await confirmLeaveLine()) return
  addingLine.value = true
  saveError.value = ''
  try {
    await addInvoiceLine(props.invoice.id, { line_kind: 'purchase' })
    emit('reload')
  } catch (error) {
    saveError.value = errorMessage(error)
  } finally { addingLine.value = false }
}

async function removeLine() {
  if (deleting.value) return
  const ok = await new Promise<boolean>((resolve) => {
    uni.showModal({
      title: '删掉这一行？',
      content: `第 ${props.line.line_no} 行是手工加的，删掉后不会留下记录。识别出来的行不能删——那种请标成「跳过」。`,
      confirmText: '删掉这一行', cancelText: '不删',
      success: (result) => resolve(Boolean(result.confirm)),
      fail: () => resolve(false)
    })
  })
  if (!ok) return
  deleting.value = true
  saveError.value = ''
  try {
    await deleteInvoiceLine(props.invoice.id, props.line.id)
    emit('reload')
  } catch (error) {
    saveError.value = errorMessage(error)
  } finally { deleting.value = false }
}

// A new line means a new draft. Only the id is watched: when the parent swaps in
// the server's version of the *same* line, the form must keep what is typed.
watch(() => props.line.id, () => {
  draft.value = draftOf(props.line)
  newCategoryId.value = null
  dismissed.value = new Set()
  activeCandidate.value = candidateFields.find((key) => suggestion(key)) ?? null
  focusField.value = null
  fieldErrors.value = {}
  saveError.value = ''
  savedNote.value = ''
  conflict.value = null
  actionError.value = ''
  actionNote.value = ''
  skipReason.value = ''
  showSkip.value = false
  mismatch.value = null
  showOverrides.value = Boolean(props.line.purchase_date || props.line.source_name || props.line.currency)
}, { immediate: true })
</script>

<template>
  <view class="editor">
    <view v-if="handAdded" class="editor-head">
      <view class="head-tags"><text class="tag">手工加的行</text></view>
    </view>

    <view v-if="readOnly" class="note" role="status">
      <text v-if="invoice.status === 'completed'">这张单据已完成；可回看已入账行和原件，不能在此修改草稿。</text>
      <text v-else-if="line.status === 'confirmed'">
        这一行已经入账（采购记录 #{{ line.purchase_id ?? '未记录' }}），草稿不能再改：改了会让草稿和采购历史两处数字对不上。
        当前界面尚不支持修改已入账历史值；请通过后端的 PATCH /api/purchases/{{ line.purchase_id }} 修正采购记录（保留修改日志），或先撤销确认再重新审核。
      </text>
      <text v-else>这张单据已作废，不能再改。</text>
    </view>
    <view v-else-if="line.status === 'skipped'" class="note" role="status">
      这一行标成了跳过{{ line.skip_reason ? `（原因：${line.skip_reason}）` : '' }}。先改回待核对才能确认入账。
    </view>

    <view v-if="conflict" class="conflict" role="alert">
      <text class="conflict-title">这一行被改过了，两边的值不一样</text>
      <text class="conflict-message">{{ conflict.message }}</text>
      <text v-if="conflict.versions" class="muted small">{{ conflict.versions }}</text>
      <view class="conflict-table">
        <view class="conflict-row heading"><text>字段</text><text>你的输入</text><text>服务端现在的值</text></view>
        <view v-for="key in changedFields(conflict.mine, line)" :key="key" class="conflict-row">
          <text>{{ FIELD_LABEL[key] }}</text>
          <text>{{ conflict.mine[key] || '（空）' }}</text>
          <text>{{ serverDraft[key] || '（空）' }}</text>
        </view>
      </view>
      <text class="muted small">两个值都可能看起来是对的，请对着原件选。</text>
      <view class="conflict-actions">
        <button class="ui-button secondary" @click="resolveWithServer">用服务端的值（丢弃我的输入）</button>
        <button class="ui-button" @click="resolveWithMine">保留我的输入，按新版本重新保存</button>
      </view>
    </view>

    <view v-if="missing.length" class="note" role="status">
      还缺 <text class="emphasis">{{ missing.map(missingLabel).join('、') }}</text>，填上才能入账；也可以写明原因跳过这一行。
    </view>
    <view v-if="generalIssues.length" class="issue-block" role="status">
      <text v-for="(issue, index) in generalIssues" :key="`general-${index}`" class="issue-text">
        待核对（{{ FIELD_LABEL[issue.field as keyof typeof FIELD_LABEL] || issue.field || '未指明字段' }}）：{{ issue.message || issue.code || '服务端标记了一个需要核对的问题' }}
      </text>
    </view>

    <view class="fields">
      <view class="field">
        <view class="field-label-row"><text class="field-label">{{ FIELD_LABEL.description }}</text><text v-if="suggestion('description')" class="ai-badge">AI 识别 · 待核对</text></view>
        <input v-model="draft.description" class="ui-input" :disabled="readOnly"
          :focus="focusField === 'description'" @focus="activeCandidate = 'description'"
          :aria-label="FIELD_LABEL.description" :placeholder="suggestion('description') || ''" />
        <view v-if="suggestion('description') && activeCandidate === 'description'" class="ai-review">
          <text>请对照原件核对</text>
          <view class="ai-actions"><button class="accept" @click="acceptSuggestion('description')">此数据正确</button><button class="edit" @click="editSuggestion('description')">修改</button></view>
        </view>
        <text v-for="(issue, index) in issues.get('description') ?? []" :key="index" class="issue-text">{{ issue.message }}</text>
        <text v-if="fieldErrors.description" class="error-text" role="alert">{{ fieldErrors.description }}</text>
      </view>

      <view class="field-grid">
        <view v-for="name in MONEY_FIELDS" :key="name" class="field">
          <view class="field-label-row"><text class="field-label">{{ FIELD_LABEL[name] }}</text><text v-if="suggestion(name)" class="ai-badge">AI 识别 · 待核对</text></view>
          <input v-model="draft[name]" class="ui-input" :disabled="readOnly" inputmode="decimal"
            :focus="focusField === name" @focus="activeCandidate = name"
            :aria-label="FIELD_LABEL[name]" :placeholder="suggestion(name) || ''" />
          <view v-if="suggestion(name) && activeCandidate === name" class="ai-review">
            <text>请对照原件核对</text>
            <view class="ai-actions"><button class="accept" @click="acceptSuggestion(name)">此数据正确</button><button class="edit" @click="editSuggestion(name)">修改</button></view>
          </view>
          <text v-for="(issue, index) in issues.get(name) ?? []" :key="index" class="issue-text">
            {{ issue.message }}<template v-if="issue.raw_text">（原文「{{ issue.raw_text }}」）</template>
          </text>
          <text v-if="fieldErrors[name]" class="error-text" role="alert">{{ fieldErrors[name] }}</text>
        </view>
        <view class="field">
          <view class="field-label-row"><text class="field-label">{{ FIELD_LABEL.purchase_unit }}</text><text v-if="suggestion('purchase_unit')" class="ai-badge">AI 识别 · 待核对</text></view>
          <input v-model="draft.purchase_unit" class="ui-input" :disabled="readOnly"
            :focus="focusField === 'purchase_unit'" @focus="activeCandidate = 'purchase_unit'"
            :aria-label="FIELD_LABEL.purchase_unit" :placeholder="suggestion('purchase_unit') || '例如：lb'" />
          <view v-if="suggestion('purchase_unit') && activeCandidate === 'purchase_unit'" class="ai-review">
            <text>请对照原件核对</text>
            <view class="ai-actions"><button class="accept" @click="acceptSuggestion('purchase_unit')">此数据正确</button><button class="edit" @click="editSuggestion('purchase_unit')">修改</button></view>
          </view>
          <text v-for="(issue, index) in issues.get('purchase_unit') ?? []" :key="index" class="issue-text">{{ issue.message }}</text>
          <text v-if="fieldErrors.purchase_unit" class="error-text" role="alert">{{ fieldErrors.purchase_unit }}</text>
        </view>
      </view>

      <button class="ui-button ghost override-toggle" :aria-pressed="showOverrides" @click="showOverrides = !showOverrides">
        {{ showOverrides ? '收起' : '这一行的日期 / 供应商 / 币种和整张单不一样？' }}
      </button>
      <view v-if="showOverrides" class="field-grid">
        <view class="field">
          <text class="field-label">{{ FIELD_LABEL.purchase_date }}</text>
          <input v-model="draft.purchase_date" class="ui-input" :disabled="readOnly"
            :aria-label="FIELD_LABEL.purchase_date" placeholder="YYYY-MM-DD；不填跟单据" />
          <text class="muted small">{{ inheritedNote('purchase_date') }}</text>
          <text v-if="fieldErrors.purchase_date" class="error-text" role="alert">{{ fieldErrors.purchase_date }}</text>
        </view>
        <view class="field">
          <text class="field-label">{{ FIELD_LABEL.source_name }}</text>
          <input v-model="draft.source_name" class="ui-input" :disabled="readOnly"
            :aria-label="FIELD_LABEL.source_name" placeholder="不填跟单据" />
          <text class="muted small">{{ inheritedNote('source_name') }}</text>
          <text v-if="fieldErrors.source_name" class="error-text" role="alert">{{ fieldErrors.source_name }}</text>
        </view>
        <view class="field">
          <text class="field-label">{{ FIELD_LABEL.currency }}</text>
          <picker :disabled="readOnly" :range="['继承单据头', 'USD', 'CNY']" :value="CURRENCIES.indexOf(draft.currency)"
            @change="draft.currency = CURRENCIES[Number($event.detail.value)] ?? ''">
            <view class="ui-input picker-text">{{ draft.currency || '继承单据头' }}</view>
          </picker>
          <text class="muted small">{{ inheritedNote('currency') }}</text>
        </view>
      </view>

      <FoodItemMatcher
        v-if="!readOnly"
        :invoice-id="invoice.id"
        :line="line"
        :invoice-source="invoice.source_name"
        :description="draft.description"
        :busy="saving"
        :doc-class="typeof invoice.recognition.raw_document_fields?.doc_class === 'string' ? invoice.recognition.raw_document_fields.doc_class : null"
        @choose="patch({ food_item_id: $event }, '已选定食材并保存。')"
        @clear="patch({ food_item_id: null }, '已取消这一行的食材。')"
        @category-change="newCategoryId = $event"
      />
      <view v-else-if="line.food_item" class="chosen-readonly">
        <text class="field-label">{{ FIELD_LABEL.food_item_id }}</text>
        <text>{{ line.food_item.name }}（{{ line.food_item.base_unit }}）</text>
      </view>
    </view>

    <text v-if="savedNote" class="saved-note" role="status" aria-live="polite">{{ savedNote }}</text>
    <text v-if="saveError" class="error-text" role="alert">{{ saveError }}</text>

    <view v-if="!readOnly" class="editor-actions">
      <view class="primary-actions">
        <button class="ui-button" :disabled="saving || !dirty" @click="patch()">
          {{ saving ? '保存中…' : dirty ? `保存这一行（${changed.length} 项修改）` : '没有未保存的修改' }}
        </button>
        <button class="ui-button secondary" :disabled="saving || !dirty" @click="discard">放弃修改</button>
      </view>
      <view class="secondary-actions">
        <button class="ui-button secondary" :disabled="addingLine" @click="addBlankLine">
          {{ addingLine ? '正在加…' : '加一行' }}
        </button>
        <button v-if="handAdded" class="ui-button ghost danger" :disabled="deleting" @click="removeLine">
          {{ deleting ? '删除中…' : '删掉这一行' }}
        </button>
      </view>
    </view>
    <view v-if="invoice.status !== 'voided' && invoice.status !== 'completed'" class="status-actions">
      <text v-if="actionNote" class="saved-note" role="status" aria-live="polite">{{ actionNote }}</text>
      <text v-if="actionError" class="error-text" role="alert">{{ actionError }}</text>
      <view v-if="mismatch" class="mismatch" role="alert">
        <text class="emphasis">行金额与数量 × 单价不符</text>
        <text>纸面行金额：{{ mismatch.line_amount ?? '未提供' }}</text>
        <text>数量 × 单价：{{ mismatch.expected ?? '未提供' }}</text>
        <text>差额：{{ mismatch.difference ?? '未提供' }}（容差 {{ mismatch.tolerance ?? '未提供' }}）</text>
        <text>请对照原件。继续后将按行金额入账，并标记金额异常。</text>
        <view class="primary-actions">
          <button class="ui-button" :disabled="actionBusy" @click="confirmLine(true)">仍按行金额入账</button>
          <button class="ui-button secondary" :disabled="actionBusy" @click="mismatch = null">返回核对数字</button>
        </view>
      </view>
      <template v-if="line.status === 'pending'">
        <view class="status-buttons">
          <button class="ui-button" :disabled="actionBusy || saving" @click="confirmLine(false)">
            {{ actionBusy ? '处理中…' : '确认此行并入账' }}
          </button>
          <button class="ui-button secondary" :disabled="actionBusy" :aria-expanded="showSkip" @click="showSkip = !showSkip">跳过此行</button>
        </view>
        <view v-if="showSkip" class="skip-form">
          <text class="field-label">跳过原因（必填）</text>
          <textarea v-model="skipReason" class="ui-input skip-input" aria-label="跳过原因" placeholder="例如：原件这一行不是采购，或无法辨认" />
          <text v-if="skipError" class="error-text" role="alert">{{ skipError }}</text>
          <button class="ui-button secondary" :disabled="actionBusy" @click="skipLine">保存跳过原因</button>
        </view>
      </template>
      <button v-else-if="line.status === 'skipped'" class="ui-button secondary" :disabled="actionBusy" @click="restoreLine">改回待核对</button>
      <view v-else-if="line.status === 'confirmed'" class="confirmed-actions">
        <text class="muted small">撤销后这条采购记录会作废。</text>
        <button class="ui-button ghost danger" :disabled="actionBusy" @click="unconfirmLine">更多操作：撤销确认</button>
      </view>
    </view>
  </view>
</template>

<style scoped>
.editor { display: flex; flex-direction: column; gap: 16px; padding: 20px; min-width: 0; }
.editor-head { display: flex; align-items: start; justify-content: space-between; gap: 12px; }
.editor-title { display: block; margin-top: 6px; font-size: 17px; font-weight: 650; }
.head-tags { flex: none; display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.tag { padding: 3px 8px; border-radius: 5px; background: #f1f3ee; color: var(--muted); font-size: 11px; }
.tag.confirmed { background: #e8f1e9; color: #155c3f; }
.tag.skipped { background: #fff5e9; color: #704007; }
.note { padding: 10px 12px; border-radius: 6px; background: #fff5e9; color: #704007; font-size: 13px; line-height: 1.55; }
.emphasis { font-weight: 700; }
.issue-block { display: flex; flex-direction: column; gap: 6px; }
.issue-text { color: #704007; font-size: 12px; line-height: 1.5; }
.fields { display: flex; flex-direction: column; gap: 16px; }
.field-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px 18px; }
.field { min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.field-label { font-size: 13px; font-weight: 650; }
.field-label-row { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.ai-badge { color: var(--muted); font-size: 11px; white-space: nowrap; }
.ui-input::placeholder { color: #78867d; opacity: 1; }
.ai-review { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 30px; color: var(--muted); font-size: 12px; }
.ai-actions { display: flex; flex-wrap: wrap; gap: 4px; }
.ai-actions button { min-height: 44px; margin: 0; padding: 7px 9px; border: 1px solid transparent; border-radius: 5px; color: #fff; font-size: 12px; cursor: pointer; }
.ai-actions button::after { border: 0; }
.ai-actions .accept { background: var(--green); }
.ai-actions .edit { background: var(--error); }
.error-text { color: var(--error); font-size: 13px; line-height: 1.5; }
.small { font-size: 12px; line-height: 1.5; }
.picker-text { display: flex; align-items: center; }
.override-toggle { align-self: flex-start; padding-left: 0; text-align: left; }
.chosen-readonly { display: flex; flex-direction: column; gap: 6px; padding: 12px; border: 1px solid var(--line); border-radius: 7px; background: #f9faf7; font-size: 14px; }
.saved-note { color: #155c3f; font-size: 13px; }
.editor-actions { display: flex; flex-direction: column; gap: 10px; padding-top: 12px; border-top: 1px solid var(--line); }
.status-actions { display: flex; flex-direction: column; gap: 10px; padding-top: 14px; border-top: 1px solid var(--line); }
.status-buttons, .confirmed-actions, .skip-form, .mismatch { display: flex; flex-direction: column; gap: 10px; }
.status-buttons { flex-direction: row; flex-wrap: wrap; }
.skip-input { min-height: 88px; width: 100%; box-sizing: border-box; }
.mismatch { padding: 14px; border: 1px solid #d6a66f; border-radius: 8px; background: #fff5e9; color: #704007; font-size: 13px; }
.confirmed-actions { align-items: flex-start; }
.primary-actions, .secondary-actions { display: flex; flex-wrap: wrap; gap: 10px; }
.danger { color: var(--error); }
.conflict { display: flex; flex-direction: column; gap: 10px; padding: 14px; border: 1px solid #d9a9a0; border-radius: 8px; background: #fdf1ef; }
.conflict-title { font-size: 14px; font-weight: 700; color: var(--error); }
.conflict-message { font-size: 13px; line-height: 1.55; }
.conflict-table { display: flex; flex-direction: column; }
.conflict-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1fr); gap: 10px; padding: 8px 0; border-bottom: 1px solid #e8cfca; font-size: 13px; }
.conflict-row text { min-width: 0; overflow-wrap: anywhere; }
.conflict-row.heading { color: var(--muted); font-size: 12px; font-weight: 650; }
.conflict-actions { display: flex; flex-wrap: wrap; gap: 10px; }
@media (max-width: 899px) {
  .editor { padding: 16px; }
  .field-grid { grid-template-columns: 1fr; }
  .primary-actions .ui-button, .secondary-actions .ui-button { flex: 1 1 auto; }
  .status-actions { position: sticky; bottom: 0; z-index: 2; background: var(--surface, #fff); padding-bottom: 12px; }
  .status-buttons .ui-button { flex: 1 1 auto; }
}
</style>
