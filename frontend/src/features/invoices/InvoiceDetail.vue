<script setup lang="ts">
/**
 * One invoice: the original on one side, what the system read on the other.
 *
 * F04 built this as a single column (header, extraction task, line overview,
 * files). F06 turns it into the review layout from the plan (section 3): the
 * original document at ~45% and everything else at ~55% on a desktop, and one
 * of the two at a time on a phone.
 *
 * Why the narrow switch is `v-show` and not `v-if`: toggling 原件 / 填写 must
 * keep the current line, the page being read and the zoom. Unmounting the
 * viewer would throw all three away, and the user would be back to finding the
 * row again — the plan calls this out explicitly for the phone layout.
 *
 * F07 adds the draft form: the invoice header stays here, and LineEditor owns
 * one line. F08 keeps counts and line states from the server after each action.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  addInvoiceLine, ApiError, completeInvoice, deleteInvoiceFile, errorMessage, getInvoice,
  getInvoiceFileContent,
  startExtraction, updateInvoice, voidInvoice,
  type ExtractionJob, type Invoice, type InvoiceHeaderInput, type InvoiceLine
} from '../../shared/api'
import { formatBytes } from '../../shared/format'
import { downloadAttachment } from '../../shared/platform/download'
import { registerLeaveGuard } from '../../shared/platform/leaveGuard'
import { isNarrowViewport, watchViewport } from '../../shared/platform/viewport'
import { readDocumentEvidence, readLineEvidence } from './evidence'
import ExtractionStatus from './ExtractionStatus.vue'
import CompletedInvoiceView from './CompletedInvoiceView.vue'
import InvoiceUpload from './InvoiceUpload.vue'
import LineEditor from './LineEditor.vue'
import LineTable from './LineTable.vue'
import OriginalDocumentViewer from './OriginalDocumentViewer.vue'

const props = defineProps<{ invoiceId: number | null }>()
const emit = defineEmits<{ back: []; saved: []; completed: []; openExisting: [id: number] }>()
const invoice = ref<Invoice | null>(null)
const state = ref<'loading' | 'ready' | 'error'>('loading')
const failure = ref('')
const saving = ref(false)
const editing = ref(false)
const saveError = ref('')
const fieldError = ref<Record<string, string>>({})
const form = ref({ source_name: '', invoice_number: '', invoice_date: '', currency: 'USD', invoice_total: '' })
const appending = ref(false)
const narrow = ref(isNarrowViewport())
const panel = ref<'document' | 'form'>('form')
const selectedLineId = ref<number | null>(null)
/** Page the user asked the viewer to show, overriding the line's own page. */
const pageOverride = ref<number | null>(null)
const showHeaderEvidence = ref(false)
/**
 * 第一眼只看识别结果。单据头、原件清单、每行的原文证据、识别诊断都折起来，
 * 老板主动点才展开——他打开这一页是来核对单子的，不是来读系统状态的。
 */
const headerOpen = ref(false)
const filesOpen = ref(false)
const diagnosticsOpen = ref(false)
/** 识别任务卡自己报它有没有话说；只有 'done' 可以藏起来。 */
const extractionActivity = ref<'loading' | 'busy' | 'failed' | 'warned' | 'done' | 'none' | 'error'>('loading')
const extractionJobs = ref<ExtractionJob[]>([])
const extractionSettling = ref(false)
const extractionSettleError = ref('')
const waitingLong = ref(false)
const originalOnly = ref(false)
const manualAfterFailure = ref(false)
let waitTimer: ReturnType<typeof setTimeout> | null = null
/**
 * The line editor, asked before the selection moves: unsaved cell edits belong
 * to it, and the plan requires that switching lines save or prompt first.
 */
const lineEditor = ref<{ confirmLeaveLine: () => Promise<boolean> } | null>(null)
const addingLine = ref(false)
const addLineError = ref('')
const closeoutOpen = ref(false)
const checkedOriginal = ref(false)
const completing = ref(false)
const closeoutError = ref('')
const closeoutSnapshot = ref('')
const moreOpen = ref(false)
const actionBusy = ref(false)
const actionError = ref('')
const actionNote = ref('')
const originalDownloadError = ref('')
const extractionStatus = ref<{ refresh: () => Promise<void> } | null>(null)
let requestSequence = 0
let stopViewport = () => {}

const extractionGate = computed<'loading' | 'busy' | 'failed' | 'error' | null>(() => {
  if (!invoice.value || invoice.value.entry_source === 'import' || invoice.value.status === 'voided') return null
  if (extractionSettling.value) return 'loading'
  if (extractionSettleError.value || extractionActivity.value === 'error') return 'error'
  if (extractionActivity.value === 'loading') return 'loading'
  if (extractionActivity.value === 'busy') return 'busy'
  // A failed attempt may leave earlier or manually entered lines intact. Once
  // there are lines to review, show them with the failure card above the form.
  if (extractionActivity.value === 'failed' && !manualAfterFailure.value && invoice.value.lines.length === 0) return 'failed'
  return null
})
const failedExtraction = computed(() => {
  const files = new Set(invoice.value?.files.map((file) => file.id) ?? [])
  const seen = new Set<number>()
  return extractionJobs.value.find((job) => {
    const id = job.invoice_file_id
    if (id === null || !files.has(id) || seen.has(id)) return false
    seen.add(id)
    return job.status === 'failed'
  }) ?? null
})
const waitingText = computed(() => extractionJobs.value.some((job) => job.status === 'running')
  ? '正在识别原件，完成后会显示完整的待核对行。'
  : '原件已保存，正在等待识别。完成后会显示完整的待核对行。')

watch(extractionGate, (gate) => {
  if (waitTimer !== null) { clearTimeout(waitTimer); waitTimer = null }
  waitingLong.value = false
  if (gate === 'busy') waitTimer = setTimeout(() => { waitingLong.value = true }, 20_000)
})

const statusLabel: Record<Invoice['status'], string> = {
  draft: '草稿', in_review: '审核中', ready_to_complete: '待完成', completed: '已完成', voided: '已作废'
}
const lineStatusLabel = { pending: '待核对', confirmed: '已入账', skipped: '已跳过' }
const hasConfirmed = computed(() => (invoice.value?.reconciliation.confirmed_count ?? 0) > 0)
const canManage = computed(() => invoice.value?.status !== 'completed' && invoice.value?.status !== 'voided')
const canComplete = computed(() => {
  const item = invoice.value
  return !!item && item.status === 'ready_to_complete'
    && item.reconciliation.line_count > 0 && item.reconciliation.pending_count === 0
})
/** 「完成单据」为什么还是灰的。没有理由就是能点了。 */
const blockedReason = computed(() => {
  const item = invoice.value
  if (!item || canComplete.value) return ''
  const pending = item.reconciliation.pending_count
  if (pending > 0) return `还有 ${pending} 行待核对`
  if (item.reconciliation.line_count === 0) return '这张单据还没有行'
  return '暂时不能完成，请重新读取详情'
})

/**
 * 只在**对不上**的时候说话。对上了，上面的「差额 0.00」已经说完了同一件事，
 * 再写一句「在容差 0.05 内对上了」只是把它翻译成系统的词。
 */
const reconciliationNote = computed(() => {
  const result = invoice.value?.reconciliation
  switch (result?.status) {
    case 'balanced': return ''
    case 'not_checked': return `还有 ${result.pending_count} 行没处理完，算不了差额。`
    case 'no_total': return '单据上没有总额，算不了差额。'
    case 'mixed_currencies': return '这张单里有多种币种，金额不能直接相加。'
    case 'currency_required': return '还没填币种，算不了差额。'
    case 'currency_mismatch': return '行的币种和单据上的不一样，算不了差额。'
    case 'mismatch': return `入账金额和单据总额差 ${result.difference ?? '未知'}，可能漏了行或者哪一行金额填错了。`
    default: return ''
  }
})

/** 折叠时也看得见的一行：不展开就知道单据头填没填。 */
const headerSummary = computed(() => {
  const item = invoice.value
  if (!item) return ''
  const parts = [item.source_name, item.invoice_date, item.invoice_number]
    .filter((value): value is string => !!value)
  const total = item.invoice_total === null ? null : `合计 ${item.invoice_total}`
  if (total) parts.push(total)
  return parts.length ? parts.join(' · ') : '来源、日期、单号、总额都还没填'
})

const lines = computed<InvoiceLine[]>(() => invoice.value?.lines ?? [])
const selectedLine = computed<InvoiceLine | null>(() =>
  lines.value.find((line) => line.id === selectedLineId.value) ?? null)
const headerEvidence = computed(() =>
  invoice.value ? readDocumentEvidence(invoice.value.recognition.raw_document_fields) : null)

/** Where the viewer should look: the selected line's evidence, unless the user moved. */
const viewerFocus = computed(() => {
  const line = selectedLine.value
  if (!line) return pageOverride.value === null ? null : { fileId: null, page: pageOverride.value }
  const evidence = readLineEvidence(line)
  return { fileId: evidence.fileId, page: pageOverride.value ?? evidence.page }
})
const documentVisible = computed(() => !narrow.value || panel.value === 'document')
const formVisible = computed(() => !narrow.value || panel.value === 'form')

function setForm(item: Invoice) {
  form.value = {
    source_name: item.source_name ?? '', invoice_number: item.invoice_number ?? '',
    invoice_date: item.invoice_date ?? '', currency: item.currency ?? 'USD', invoice_total: item.invoice_total ?? ''
  }
}

/** True while the edit form differs from what the server last returned. */
const headerDirty = computed(() => {
  const item = invoice.value
  if (!editing.value || !item) return false
  return form.value.source_name !== (item.source_name ?? '')
    || form.value.invoice_number !== (item.invoice_number ?? '')
    || form.value.invoice_date !== (item.invoice_date ?? '')
    || form.value.currency !== (item.currency ?? 'USD')
    || form.value.invoice_total !== (item.invoice_total ?? '')
})

// Leaving the detail with an unsaved header edit asks first (plan section 6).
const stopGuard = registerLeaveGuard(
  () => (headerDirty.value ? '单据头的修改还没有保存。离开会丢掉这些修改。' : null),
  () => { editing.value = false; if (invoice.value) setForm(invoice.value) }
)
onUnmounted(() => {
  stopGuard(); stopViewport()
  if (waitTimer !== null) clearTimeout(waitTimer)
})
onMounted(() => { stopViewport = watchViewport((value) => { narrow.value = value }) })

/** Keeps the selected line across re-reads; defaults to the first pending one. */
function keepSelection(item: Invoice) {
  if (item.lines.some((line) => line.id === selectedLineId.value)) return
  const pending = item.lines.find((line) => line.status === 'pending')
  selectedLineId.value = (pending ?? item.lines[0])?.id ?? null
  pageOverride.value = null
}

async function load() {
  const sequence = ++requestSequence
  if (props.invoiceId === null) {
    state.value = 'error'
    failure.value = '链接中没有有效的单据 ID。请返回单据列表重新打开。'
    return
  }
  state.value = 'loading'
  failure.value = ''
  try {
    const item = await getInvoice(props.invoiceId)
    if (sequence !== requestSequence) return
    invoice.value = item
    keepSelection(item)
    if (!editing.value) setForm(item)
    state.value = 'ready'
  } catch (error) {
    if (sequence !== requestSequence) return
    invoice.value = null
    failure.value = errorMessage(error)
    state.value = 'error'
  }
}

/**
 * Re-read without flipping the view back to its loading state.
 *
 * `load()` replaces the whole pane with "正在读取…", which unmounts the editor
 * and the original viewer — that is right for opening an invoice and wrong for
 * refreshing after a save: it would throw away the version-conflict panel the
 * refresh was fetching the data *for*, along with the page and zoom the user
 * had set on the paper.
 */
async function refreshQuiet() {
  if (props.invoiceId === null) return
  const sequence = ++requestSequence
  try {
    const item = await getInvoice(props.invoiceId)
    if (sequence !== requestSequence) return
    invoice.value = item
    keepSelection(item)
    if (!editing.value) setForm(item)
    state.value = 'ready'
    addLineError.value = ''
    emit('saved')
  } catch (error) {
    if (sequence !== requestSequence) return
    // The invoice on screen is still the last good read, so it stays; only the
    // refresh failed, and saying which is the difference between "your edit was
    // lost" and "the list may be a few seconds stale".
    addLineError.value = `重新读取单据失败：${errorMessage(error).replace(/[。！!？?]+$/, '')}。界面上的内容是上一次读到的，请重试读取。`
  }
}

/** One line replaced by the server's stored version, including its new `version`. */
function applyLine(stored: InvoiceLine) {
  const item = invoice.value
  if (!item) return
  invoice.value = {
    ...item,
    lines: item.lines.map((line) => (line.id === stored.id ? stored : line))
  }
  emit('saved')
}

function applyStoredInvoice(stored: Invoice, advance: boolean) {
  invoice.value = stored
  if (advance && selectedLineId.value !== null) {
    const index = stored.lines.findIndex((line) => line.id === selectedLineId.value)
    const current = index >= 0 ? stored.lines[index] : undefined
    if (current && current.status !== 'pending') {
      // 审一张单子是从上往下走一遍，所以先往后找；后面没有了再回头找前面漏下的。
      const after = stored.lines.slice(index + 1).find((line) => line.status === 'pending')
      const before = stored.lines.slice(0, index).find((line) => line.status === 'pending')
      selectedLineId.value = (after ?? before)?.id ?? null
    }
  }
  keepSelection(stored)
  emit('saved')
}

async function selectLine(line: InvoiceLine) {
  // 再点一次已经展开的行 = 收起它，好让人一眼扫完整张单据的识别结果。
  // 收起同样要先过未保存提示：折叠和切到别的行一样会让输入离开视野。
  if (selectedLineId.value === line.id) {
    if (lineEditor.value && !await lineEditor.value.confirmLeaveLine()) return
    selectedLineId.value = null
    return
  }
  if (lineEditor.value && !await lineEditor.value.confirmLeaveLine()) return
  selectedLineId.value = line.id
  // The new line carries its own page; a page the user picked for the old line
  // must not follow them to the next one.
  pageOverride.value = null
}

/** Add an empty line and select it, so the user can start filling cells at once. */
async function addLine() {
  if (addingLine.value || !invoice.value) return
  if (lineEditor.value && !await lineEditor.value.confirmLeaveLine()) return
  addingLine.value = true
  addLineError.value = ''
  try {
    const created = await addInvoiceLine(invoice.value.id, { line_kind: 'purchase' })
    selectedLineId.value = created.id
    pageOverride.value = null
    if (narrow.value) panel.value = 'form'
    await refreshQuiet()
    selectedLineId.value = created.id
  } catch (error) {
    addLineError.value = errorMessage(error)
  } finally { addingLine.value = false }
}

function beginEdit() {
  if (!invoice.value) return
  setForm(invoice.value)
  fieldError.value = {}
  saveError.value = ''
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  saveError.value = ''
  fieldError.value = {}
  if (invoice.value) setForm(invoice.value)
}

function nullable(value: string): string | null { return value.trim() || null }

async function save() {
  if (!invoice.value || saving.value) return
  fieldError.value = {}
  saveError.value = ''
  const data: InvoiceHeaderInput = {
    source_name: nullable(form.value.source_name), invoice_number: nullable(form.value.invoice_number),
    invoice_date: nullable(form.value.invoice_date), currency: form.value.currency || 'USD',
    invoice_total: nullable(form.value.invoice_total)
  }
  if (data.invoice_total !== null && !/^\d+(?:\.\d{1,2})?$/.test(data.invoice_total)) {
    fieldError.value = { invoice_total: '请输入非负金额，最多两位小数；留空表示未知。' }
    return
  }
  saving.value = true
  try {
    await updateInvoice(invoice.value.id, data)
    // The edit is committed: drop the dirty state before reading back, so a slow
    // re-read cannot raise a "unsaved changes" prompt for changes already saved.
    editing.value = false
    await load()
    if (state.value === 'ready') {
      emit('saved')
    } else {
      saveError.value = `保存请求已返回，但重新读取失败：${failure.value}。请重试读取。`
    }
  } catch (error) {
    saveError.value = errorMessage(error)
    if (error instanceof ApiError && typeof error.details?.field === 'string') {
      fieldError.value = { [error.details.field]: error.message }
    }
    if (error instanceof ApiError && error.status === 409) {
      saveError.value += ' 请重新读取单据，再核对修改。'
    }
  } finally { saving.value = false }
}

/** Files added from the detail view: re-read so page order and counts are the server's. */
async function filesStored() {
  await load()
  emit('saved')
}

function confirmAction(title: string, content: string): Promise<boolean> {
  return new Promise((resolve) => uni.showModal({
    title, content, confirmText: '继续', cancelText: '取消',
    success: (result) => resolve(Boolean(result.confirm)), fail: () => resolve(false)
  }))
}

async function prepareAction(): Promise<boolean> {
  if (actionBusy.value || !invoice.value || !canManage.value) return false
  actionError.value = ''
  actionNote.value = ''
  if (headerDirty.value) {
    actionError.value = '单据头有未保存的修改，请先保存或取消。'
    return false
  }
  return !lineEditor.value || await lineEditor.value.confirmLeaveLine()
}

async function syncAfterAction() {
  await refreshQuiet()
  await extractionStatus.value?.refresh()
}

/** Keep the review DOM absent until the finished job's lines have been reread. */
async function extractionFinished() {
  if (props.invoiceId === null) return
  extractionSettling.value = true
  extractionSettleError.value = ''
  const sequence = ++requestSequence
  try {
    const item = await getInvoice(props.invoiceId)
    if (sequence !== requestSequence) return
    invoice.value = item
    keepSelection(item)
    if (!editing.value) setForm(item)
    emit('saved')
  } catch (error) {
    if (sequence === requestSequence) extractionSettleError.value = errorMessage(error)
  } finally {
    if (sequence === requestSequence) extractionSettling.value = false
  }
}

async function retryExtraction() {
  if (!await prepareAction() || !invoice.value) return
  const id = invoice.value.id
  if (!await confirmAction('重新提取', '重新提取会替换未修改的识别草稿；已修改但未入账的行会保留，仍需再次核对。已有任务运行或已入账行时，服务端会拒绝。')) return
  actionBusy.value = true
  extractionSettling.value = true
  originalOnly.value = false
  manualAfterFailure.value = false
  try {
    await startExtraction(id)
    actionNote.value = '已请求重新提取。请查看识别任务状态，完成后逐行核对。'
  } catch (error) {
    actionError.value = `${errorMessage(error)} 请按最新状态处理后再试。`
  } finally {
    await syncAfterAction()
    extractionSettling.value = false
    actionBusy.value = false
  }
}

async function enterManualReview() {
  manualAfterFailure.value = true
  originalOnly.value = false
  await addLine()
}

async function removeFile(fileId: number, name: string) {
  if (!await prepareAction() || !invoice.value) return
  const id = invoice.value.id
  if (!await confirmAction('删除原件', `确定删除「${name}」？文件字节会删除，已有草稿行和识别任务历史仍保留。请先确认这不是最后一份原件。`)) return
  actionBusy.value = true
  try {
    invoice.value = await deleteInvoiceFile(id, fileId)
    actionNote.value = '文件已删除；请核对引用该原件的草稿行。'
    emit('saved')
  } catch (error) {
    actionError.value = `${errorMessage(error)} 请查看最新文件与任务状态。`
  } finally {
    await syncAfterAction()
    actionBusy.value = false
  }
}

async function cancelInvoice() {
  if (!await prepareAction() || !invoice.value) return
  const id = invoice.value.id
  if (!await confirmAction('删除整张单据', '这张单据和它的原件都会删掉，同一个文件之后可以重新上传。')) return
  const bookedIds = invoice.value.lines.filter((line) => line.status === 'confirmed').map((line) => line.id)
  if (bookedIds.length && !await confirmAction(
    '确认移除已入账数据',
    `这张单据有 ${bookedIds.length} 行已经入账。确认删除后，这些采购记录会作废，不再计入价格统计；原件也会删除。是否确认移除？`
  )) return
  actionBusy.value = true
  try {
    const result = await voidInvoice(id, bookedIds.length ? bookedIds : undefined)
    invoice.value = result.invoice
    if (result.status !== 'voided') throw new Error('服务端未确认作废，请重新读取单据。')
    moreOpen.value = false
    actionNote.value = '单据已删除。'
    emit('saved')
  } catch (error) {
    actionError.value = `${errorMessage(error)} 请根据最新状态处理后再试。`
  } finally {
    await syncAfterAction()
    actionBusy.value = false
  }
}

function locatePending(item: Invoice, numbers?: unknown) {
  const pending = item.lines.find((line) => line.status === 'pending'
    && (!Array.isArray(numbers) || numbers.includes(line.line_no)))
  if (pending) {
    selectedLineId.value = pending.id
    pageOverride.value = null
    if (narrow.value) panel.value = 'form'
  }
}

function closeoutValues(item: Invoice): string {
  return JSON.stringify({
    files: item.files.map((file) => [file.id, file.page_order]),
    lines: item.lines.map((line) => [line.id, line.status, line.version]),
    reconciliation: item.reconciliation
  })
}

async function openCloseout() {
  if (!invoice.value || closeoutOpen.value) return
  closeoutError.value = ''
  if (headerDirty.value) {
    closeoutError.value = '单据头还有未保存的修改。请先保存或取消，再检查收口摘要。'
    return
  }
  if (lineEditor.value && !await lineEditor.value.confirmLeaveLine()) return
  try {
    const item = await getInvoice(invoice.value.id)
    invoice.value = item
    keepSelection(item)
    if (item.status === 'completed') { emit('completed'); return }
    if (!canComplete.value) {
      locatePending(item)
      closeoutError.value = item.reconciliation.pending_count > 0
        ? `还有 ${item.reconciliation.pending_count} 行待核对，请逐行确认或跳过。`
        : '单据尚不能完成；请先添加并处理至少一行。'
      return
    }
    checkedOriginal.value = false
    closeoutSnapshot.value = closeoutValues(item)
    closeoutOpen.value = true
  } catch (error) { closeoutError.value = `读取最新对账结果失败：${errorMessage(error)}` }
}

async function finishInvoice() {
  if (!invoice.value || !checkedOriginal.value || completing.value) return
  completing.value = true
  closeoutError.value = ''
  try {
    const latest = await getInvoice(invoice.value.id)
    invoice.value = latest
    if (latest.status === 'completed') { emit('completed'); return }
    if (!canComplete.value) {
      closeoutOpen.value = false
      checkedOriginal.value = false
      locatePending(latest)
      closeoutError.value = '单据状态已变化，请处理待核对行后重新检查收口摘要。'
      return
    }
    if (closeoutValues(latest) !== closeoutSnapshot.value) {
      closeoutOpen.value = false
      checkedOriginal.value = false
      closeoutError.value = '单据内容已变化，请重新检查最新的原件、行数和对账结果。'
      return
    }
    const outcome = await completeInvoice(latest.id)
    invoice.value = outcome.invoice
    if (outcome.status !== 'completed' || outcome.invoice.status !== 'completed') {
      throw new Error('服务端未确认完成，请重新读取单据。')
    }
    emit('completed')
  } catch (error) {
    closeoutError.value = errorMessage(error)
    if (error instanceof ApiError && error.status === 409) {
      const numbers = error.details?.pending_line_nos
      try {
        const latest = await getInvoice(invoice.value.id)
        invoice.value = latest
        closeoutOpen.value = false
        checkedOriginal.value = false
        locatePending(latest, numbers)
        if (Array.isArray(numbers) && numbers.length) {
          closeoutError.value += ` 请处理第 ${numbers.join('、')} 行。已定位到第一条未处理行。`
        }
      } catch { closeoutError.value += ' 请重新读取详情。' }
    } else {
      // A lost response may follow a successful POST. Re-read before offering a retry.
      try {
        const latest = await getInvoice(invoice.value.id)
        invoice.value = latest
        if (latest.status === 'completed') emit('completed')
      } catch { closeoutError.value += ' 请重新读取详情确认是否已完成。' }
    }
  } finally { completing.value = false }
}

async function downloadOriginal(fileId: number, filename: string) {
  if (!invoice.value) return
  originalDownloadError.value = ''
  try {
    downloadAttachment(await getInvoiceFileContent(invoice.value.id, fileId), filename)
  } catch (error) { originalDownloadError.value = errorMessage(error) }
}

watch(() => props.invoiceId, () => {
  extractionActivity.value = 'loading'
  extractionJobs.value = []
  extractionSettling.value = false
  extractionSettleError.value = ''
  waitingLong.value = false
  originalOnly.value = false
  manualAfterFailure.value = false
  editing.value = false
  appending.value = false
  selectedLineId.value = null
  pageOverride.value = null
  panel.value = 'form'
  closeoutOpen.value = false
  headerOpen.value = false
  filesOpen.value = false
  diagnosticsOpen.value = false
  checkedOriginal.value = false
  closeoutError.value = ''
  moreOpen.value = false
  actionError.value = ''
  actionNote.value = ''
  void load()
}, { immediate: true })
</script>

<template>
  <CompletedInvoiceView v-if="invoice && state === 'ready' && invoice.status === 'completed'" :invoice="invoice" @back="emit('back')" @open-existing="emit('openExisting', $event)" @currency-changed="applyStoredInvoice($event, false)" />
  <view v-else class="detail-layout" :class="{ narrow }">
    <view class="detail-heading">
      <view class="heading-primary"><button class="back-link" @click="emit('back')">← 返回单据</button><text class="page-title">单据{{ invoice ? ` #${invoice.id}` : '' }}</text></view>
      <view v-if="invoice && canManage && !extractionGate" class="more-actions">
        <button class="ui-button ghost" :aria-expanded="moreOpen" @click="moreOpen = !moreOpen">单据更多操作</button>
        <view v-if="moreOpen" class="ui-card section more-menu">
          <view class="form-actions">
            <button v-if="invoice.entry_source !== 'import'" class="ui-button secondary" :disabled="actionBusy" @click="retryExtraction">重新提取 / 失败重试</button>
            <button v-if="invoice.entry_source !== 'import'" class="ui-button secondary" :aria-pressed="diagnosticsOpen" @click="diagnosticsOpen = !diagnosticsOpen">{{ diagnosticsOpen ? '收起识别任务' : '识别任务与诊断' }}</button>
            <button class="ui-button ghost" :disabled="actionBusy" @click="cancelInvoice">删除整张单据</button>
          </view>
          <view v-if="invoice.files.length" class="file-delete-list">
            <text class="muted">删除拍坏文件</text>
            <button v-for="file in invoice.files" :key="file.id" class="ui-button ghost" :disabled="actionBusy" @click="removeFile(file.id, file.original_name)">删除第 {{ file.page_order }} 页 · {{ file.original_name }}</button>
          </view>
        </view>
      </view>
    </view>
    <view v-if="state === 'loading'" class="state-card" role="status" aria-live="polite">正在读取单据详情…</view>
    <view v-else-if="state === 'error'" class="state-card" role="alert"><text>单据详情读取失败：{{ failure }}</text><button v-if="invoiceId !== null" class="ui-button secondary" @click="load">重试读取</button></view>
    <template v-else-if="invoice">
      <!-- Keep the poller mounted while the review DOM is absent. -->
      <ExtractionStatus
        v-if="invoice.entry_source !== 'import'"
        ref="extractionStatus"
        :invoice-id="invoice.id"
        :files="invoice.files"
        :is-fake="invoice.recognition.is_fake"
        :read-only="!canManage"
        :hidden="extractionGate !== null || (extractionActivity === 'done' && !diagnosticsOpen)"
        @finished="extractionFinished"
        @activity="extractionActivity = $event"
        @jobs="extractionJobs = $event"
      />
      <view v-if="extractionGate" class="extraction-screen">
        <template v-if="originalOnly && (extractionGate === 'busy' || extractionGate === 'failed')">
          <view class="escape-actions"><button class="ui-button secondary" @click="originalOnly = false">返回识别状态</button></view>
          <view class="ui-card original-only"><OriginalDocumentViewer :invoice-id="invoice.id" :files="invoice.files" :is-fake="invoice.recognition.is_fake" :focus="null" :visible="true" /></view>
        </template>
        <view v-else-if="extractionGate === 'busy'" class="extraction-wait" role="status" aria-live="polite">
          <view class="wait-indicator" aria-hidden="true" />
          <text class="wait-title">{{ waitingText }}</text>
          <text class="muted">{{ invoice.source_name || '来源待填' }} · {{ invoice.files.length }} 个原件文件</text>
          <view v-if="waitingLong" class="wait-escape">
            <text>先去忙别的，识别完会留在待办里。</text>
            <view class="escape-actions">
              <button class="ui-button secondary" @click="emit('back')">返回单据列表</button>
              <button class="ui-button secondary" @click="originalOnly = true">只看原件</button>
              <button class="ui-button ghost" @click="extractionStatus?.refresh()">刷新识别状态</button>
            </view>
          </view>
        </view>
        <view v-else-if="extractionGate === 'failed'" class="extraction-wait failed-screen" role="alert">
          <text class="wait-title">原件识别失败</text>
          <text>{{ failedExtraction?.error_message || '识别未完成，原件已保存。' }}</text>
          <text class="muted">{{ invoice.source_name || '来源待填' }} · {{ invoice.files.length }} 个原件文件</text>
          <view class="escape-actions">
            <button class="ui-button secondary" @click="originalOnly = true">查看原件</button>
            <button class="ui-button secondary" @click="enterManualReview">加一行</button>
            <button class="ui-button" :disabled="actionBusy" @click="retryExtraction">重试提取</button>
          </view>
          <text v-if="actionError" class="field-error">{{ actionError }}</text>
        </view>
        <view v-else-if="extractionGate === 'error'" class="state-card" role="alert">
          <text>读取识别状态失败{{ extractionSettleError ? `：${extractionSettleError}` : '，请重试。' }}</text>
          <button class="ui-button secondary" @click="extractionSettleError ? extractionFinished() : extractionStatus?.refresh()">重试读取</button>
        </view>
        <view v-else class="state-card" role="status" aria-live="polite">正在核对单据状态…</view>
      </view>
      <template v-else>
      <view v-if="extractionActivity === 'failed' && invoice.lines.length > 0" class="ui-card section failed-review" role="alert">
        <text>有原件识别失败：{{ failedExtraction?.error_message || '请核对已有行与原件。' }}</text>
        <view class="escape-actions">
          <button class="ui-button secondary" @click="panel = 'document'">查看原件</button>
          <button class="ui-button secondary" @click="addLine">加一行</button>
          <button class="ui-button" :disabled="actionBusy" @click="retryExtraction">重试提取</button>
        </view>
      </view>
      <view v-if="invoice.possible_duplicate_invoice_ids.length" class="ui-card section" role="status">
        <text class="history-note">这张单据的来源、单号和总额与以下单据相同；请核对是否重复上传。不会自动阻断审核。</text>
        <view class="form-actions"><button v-for="id in invoice.possible_duplicate_invoice_ids" :key="id" class="ui-button secondary" @click="emit('openExisting', id)">打开可能重复的单据 #{{ id }}</button></view>
      </view>
      <text v-if="actionError" class="field-error" role="alert">{{ actionError }}</text>
      <text v-if="actionNote" class="history-note" role="status">{{ actionNote }}</text>
      <view v-if="narrow" class="panel-tabs" aria-label="审核视图">
        <button :class="{ active: panel === 'document' }" :aria-current="panel === 'document' ? 'true' : undefined" @click="panel = 'document'">原件</button>
        <button :class="{ active: panel === 'form' }" :aria-current="panel === 'form' ? 'true' : undefined" @click="panel = 'form'">填写</button>
      </view>

      <view class="review-grid">
        <view v-show="documentVisible" class="ui-card document-pane">
          <view v-if="invoice.entry_source === 'import'" class="section">
            <text class="section-title">原始表格</text>
            <text class="muted">表格没有原图，下载原始文件核对。</text>
            <button v-for="file in invoice.files" :key="file.id" class="ui-button secondary" @click="downloadOriginal(file.id, file.original_name)">下载 {{ file.original_name }}</button>
            <text v-if="originalDownloadError" class="field-error" role="alert">{{ originalDownloadError }}</text>
          </view>
          <OriginalDocumentViewer
            v-else
            :invoice-id="invoice.id"
            :files="invoice.files"
            :is-fake="invoice.recognition.is_fake"
            :focus="viewerFocus"
            :visible="documentVisible"
          />
        </view>

        <view v-show="formVisible" class="form-pane">
          <!-- 主角：识别出来的东西。 -->
          <LineTable
            :invoice="invoice"
            :selected-line-id="selectedLineId"
            :narrow="narrow"
            :can-manage="canManage"
            :adding-line="addingLine"
            @select="selectLine"
            @add-line="addLine">
            <!-- 草稿表单长在它自己那一行里：改第 4 行不用滚到页面下半部分再滚回来。 -->
            <template #editor="{ line }">
              <LineEditor
                ref="lineEditor"
                :key="line.id"
                :invoice="invoice"
                :line="line"
                @applied="applyLine"
                @stored="applyStoredInvoice"
                @reload="refreshQuiet"
              />
            </template>
          </LineTable>
          <text v-if="addLineError" class="field-error" role="alert">{{ addLineError }}</text>

          <!-- 收口：对账数字只在真要完成时出现，平时不占地方。 -->
          <view v-if="invoice.reconciliation.line_count > 0" class="ui-card section" role="region" aria-label="完成单据">
            <text v-if="invoice.status === 'completed'" class="muted">单据已完成。</text>
            <text v-else-if="invoice.status === 'voided'" class="muted">单据已作废。</text>
            <template v-else>
              <button v-if="!closeoutOpen" class="ui-button" :disabled="!canComplete"
                :aria-disabled="!canComplete" @click="openCloseout">检查原件并完成单据</button>
              <text v-if="!closeoutOpen && blockedReason" class="muted blocked-reason">{{ blockedReason }}</text>
              <view v-if="closeoutOpen && canComplete" class="closeout-panel">
                <text class="section-title">最后核对</text>
                <view class="summary-grid">
                  <view><text class="label">原件文件</text><text>{{ invoice.files.length }} 个</text></view>
                  <view><text class="label">已入账 / 已跳过</text><text>{{ invoice.reconciliation.confirmed_count }} / {{ invoice.reconciliation.skipped_count }} 行</text></view>
                  <view><text class="label">单据总额</text><text>{{ invoice.reconciliation.invoice_total ?? '未知' }}</text></view>
                  <view><text class="label">已入账金额</text><text>{{ invoice.reconciliation.confirmed_amount ?? '无法汇总' }}</text></view>
                  <view><text class="label">差额</text><text>{{ invoice.reconciliation.difference ?? '无法计算' }}</text></view>
                </view>
                <view v-if="invoice.reconciliation.status === 'mixed_currencies' || invoice.reconciliation.by_category.length || invoice.reconciliation.adjustment_total || invoice.reconciliation.notes.length" class="category-breakdown" aria-label="这张单花在哪">
                  <text v-if="invoice.reconciliation.by_category.length" class="section-title">这张单花在哪</text>
                  <text v-if="invoice.reconciliation.status === 'mixed_currencies'" class="muted">这张单有多种币种，不按分类汇总。</text>
                  <template v-else>
                    <view v-if="invoice.reconciliation.by_category.length" class="category-list">
                      <view v-for="category in invoice.reconciliation.by_category" :key="category.category_id ?? category.name" class="category-row">
                        <text class="category-name">{{ category.name_zh || category.name }}<text v-if="category.name_zh" class="category-name-en"> · {{ category.name }}</text></text>
                        <text class="category-count">{{ category.line_count }} 行</text>
                        <text class="category-amount">{{ category.amount }}</text>
                      </view>
                    </view>
                    <view v-if="invoice.reconciliation.adjustment_total" class="adjustment-row">
                      <text class="category-name">运费 / 折让</text>
                      <text class="category-count">{{ invoice.reconciliation.adjustment_total.line_count }} 行</text>
                      <text class="category-amount">{{ invoice.reconciliation.adjustment_total.amount }}</text>
                    </view>
                  </template>
                  <text v-for="(note, index) in invoice.reconciliation.notes" :key="index" class="category-note">{{ note.replace(/\*\*/g, '') }}</text>
                </view>
                <text v-if="reconciliationNote" class="warning-note" role="status">{{ reconciliationNote }}</text>
                <label class="check-line" tabindex="0" role="checkbox" :aria-checked="checkedOriginal"
                  @keydown.enter.prevent="checkedOriginal = !checkedOriginal"
                  @keydown.space.prevent="checkedOriginal = !checkedOriginal">
                  <checkbox :checked="checkedOriginal" @click="checkedOriginal = !checkedOriginal" /><text>我已检查原件有无漏行</text>
                </label>
                <view class="form-actions"><button class="ui-button" :disabled="!checkedOriginal || completing" @click="finishInvoice">{{ completing ? '正在完成…' : '完成单据' }}</button><button class="ui-button secondary" :disabled="completing" @click="closeoutOpen = false; checkedOriginal = false">取消</button></view>
              </view>
            </template>
            <text v-if="closeoutError" class="field-error" role="alert">{{ closeoutError }}</text>
          </view>

          <!-- 单据头：老板不改就不用看。折叠成一行。 -->
          <view class="ui-card section fold">
            <button class="fold-toggle" :aria-expanded="headerOpen" @click="headerOpen = !headerOpen">
              {{ headerOpen ? '收起单据信息' : '单据信息' }}
              <text class="fold-summary">{{ headerSummary }}</text>
            </button>
            <template v-if="headerOpen">
              <text v-if="invoice.entry_source === 'import'" class="history-note">表格导入批次，原始文件已保存。</text>
              <text v-if="invoice.recognition.is_fake" class="history-note">演示识别结果：草稿内容来自 Fake 识别器，请按原件核对。</text>
              <view v-if="!editing" class="header-grid">
                <view><text class="label">来源</text><text>{{ invoice.source_name || '待填' }}</text></view>
                <view><text class="label">单号</text><text>{{ invoice.invoice_number || '待填' }}</text></view>
                <view><text class="label">日期</text><text>{{ invoice.invoice_date || '待填' }}</text></view>
                <view><text class="label">币种</text><text>{{ invoice.currency || 'USD（默认）' }}</text></view>
                <view><text class="label">总额</text><text>{{ invoice.invoice_total === null ? '待填' : invoice.invoice_total }}</text></view>
              </view>
              <button v-if="!editing && canManage" class="ui-button secondary" @click="beginEdit">修改单据信息</button>
              <view v-else-if="editing" class="edit-form">
                <view class="header-grid">
                  <view class="field"><text class="label">来源</text><input v-model="form.source_name" class="ui-input" aria-label="来源" placeholder="待填" /><text v-if="fieldError.source_name" class="field-error">{{ fieldError.source_name }}</text></view>
                  <view class="field"><text class="label">单号</text><input v-model="form.invoice_number" class="ui-input" aria-label="单号" placeholder="待填" /><text v-if="fieldError.invoice_number" class="field-error">{{ fieldError.invoice_number }}</text></view>
                  <view class="field"><text class="label">日期（YYYY-MM-DD）</text><input v-model="form.invoice_date" class="ui-input" aria-label="日期" placeholder="YYYY-MM-DD；留空表示未知" /><text v-if="fieldError.invoice_date" class="field-error">{{ fieldError.invoice_date }}</text></view>
                  <view class="field"><text class="label">币种（默认美元，可修改）</text><picker :range="['USD', 'CNY']" :value="['USD', 'CNY'].indexOf(form.currency)" @change="form.currency = ['USD', 'CNY'][Number($event.detail.value)]"><view class="ui-input picker-text">{{ form.currency }}</view></picker><text v-if="fieldError.currency" class="field-error">{{ fieldError.currency }}</text></view>
                  <view class="field"><text class="label">总额</text><input v-model="form.invoice_total" class="ui-input" aria-label="总额" inputmode="decimal" placeholder="待填" /><text v-if="fieldError.invoice_total" class="field-error">{{ fieldError.invoice_total }}</text></view>
                </view>
                <text v-if="hasConfirmed" class="history-note">改这里不会影响已经入账的行。</text>
                <text v-if="headerDirty" class="muted" role="status">有未保存的修改。</text>
                <text v-if="saveError" class="field-error" role="alert">{{ saveError }}</text>
                <view class="form-actions"><button class="ui-button" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存并重新读取' }}</button><button class="ui-button secondary" :disabled="saving" @click="cancelEdit">取消</button></view>
              </view>

              <template v-if="headerEvidence && headerEvidence.fields.length">
                <button class="ui-button ghost evidence-toggle" :aria-pressed="showHeaderEvidence" @click="showHeaderEvidence = !showHeaderEvidence">
                  {{ showHeaderEvidence ? '收起单据头原文' : '单据头原文' }}
                </button>
                <view v-if="showHeaderEvidence" class="header-evidence">
                  <view v-for="field in headerEvidence.fields" :key="field.key" class="row">
                    <text>{{ field.label }}：{{ field.rawText === null ? '原文为空' : field.rawText }}</text>
                    <text>{{ field.status === 'recognized' ? '识别出内容' : field.status === 'unreadable' ? '认不出' : '单据上没有' }}</text>
                  </view>
                </view>
              </template>
            </template>
          </view>

          <!-- 原件文件清单与补传：也折起来。原件本身在左边一直看得见。 -->
          <view class="ui-card section fold">
            <button class="fold-toggle" :aria-expanded="filesOpen" @click="filesOpen = !filesOpen">
              {{ filesOpen ? '收起原件文件' : '原件文件' }}
              <text class="fold-summary">{{ invoice.files.length }} 个</text>
            </button>
            <template v-if="filesOpen">
              <view v-if="invoice.files.length" class="rows">
                <view v-for="file in invoice.files" :key="file.id" class="row">
                  <text>第 {{ file.page_order }} 页 · {{ file.original_name }}</text>
                  <text>{{ formatBytes(file.size) }}</text>
                  <button v-if="invoice.entry_source === 'import'" class="ui-button secondary" @click="downloadOriginal(file.id, file.original_name)">下载原始表格</button>
                </view>
              </view>
              <text v-else class="muted">暂无原件文件。</text>
              <text v-if="originalDownloadError" class="field-error" role="alert">{{ originalDownloadError }}</text>
              <button v-if="canManage" class="ui-button secondary" :aria-pressed="appending" @click="appending = !appending">{{ appending ? '收起补传' : '补传文件' }}</button>
              <InvoiceUpload
                v-if="appending"
                mode="append"
                :invoice-id="invoice.id"
                :existing-files="invoice.files.length"
                @stored="filesStored"
                @open-existing="emit('openExisting', $event)"
              />
            </template>
          </view>
        </view>
      </view>
      </template>
    </template>
  </view>
</template>

<style scoped>
/* 行内展开的那一块：表单与证据直接排在这一行下面，不再是一张独立的卡。 */
/* 折叠卡：收起时就是一行按钮，不占版面。 */
.fold { gap: 10px; padding: 14px 18px; }
.fold-toggle {
  display: flex; align-items: center; gap: 10px; width: 100%; min-height: 44px; margin: 0; padding: 0;
  border: 0; background: transparent; color: var(--green); font-size: 15px; font-weight: 600; text-align: left; cursor: pointer;
}
.fold-toggle::after { border: 0; }
.fold-summary { color: var(--muted); font-weight: 400; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.detail-layout { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.detail-heading { position: relative; display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 48px; }
.heading-primary { display: flex; align-items: center; gap: 12px; min-width: 0; }
.back-link { min-height: 44px; margin: 0; padding: 8px 10px; border: 0; background: transparent; color: var(--green); text-align: left; }
.back-link::after { border: 0; }
.page-title { display: block; font-size: 23px; font-weight: 700; white-space: nowrap; }
.state-card { min-height: 140px; display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 12px; padding: 24px; border: 1px dashed var(--line); border-radius: 9px; text-align: center; }
.extraction-screen { min-width: 0; }
.extraction-wait { min-height: min(70vh, 560px); display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 16px; padding: 24px; text-align: center; overflow-wrap: anywhere; }
.wait-title { font-size: 19px; font-weight: 650; line-height: 1.5; }
.wait-indicator { width: 38px; height: 38px; border: 4px solid #dce9df; border-top-color: var(--green); border-radius: 50%; animation: wait-spin 1s linear infinite; }
@keyframes wait-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .wait-indicator { animation: none; border-color: var(--green); } }
.wait-escape { display: flex; flex-direction: column; align-items: center; gap: 12px; max-width: 100%; }
.escape-actions { display: flex; justify-content: center; flex-wrap: wrap; gap: 10px; max-width: 100%; }
.escape-actions .ui-button { min-height: 44px; max-width: 100%; white-space: normal; }
.failed-screen { min-height: min(60vh, 480px); }
.failed-review { border-color: var(--error); }
.original-only { min-width: 0; margin-top: 12px; overflow: hidden; }
.original-only > * { min-height: 60vh; }
.panel-tabs { display: flex; padding: 4px; border-radius: 9px; background: #e5ebe2; }
.panel-tabs button { flex: 1; min-height: 44px; margin: 0; border: 0; border-radius: 7px; background: transparent; color: var(--muted); font-size: 14px; cursor: pointer; }
.panel-tabs button::after { border: 0; }
.panel-tabs button.active { background: #fff; color: var(--ink); font-weight: 700; }
/* Desktop: original and form side by side, each with its own scroll, so
   switching lines never scrolls the original back to the top. */
.review-grid { min-width: 0; height: max(520px, calc(100vh - 104px)); display: grid; grid-template-columns: minmax(0, 45fr) minmax(0, 55fr); align-items: start; gap: 20px; }
.document-pane { min-width: 0; height: 100%; overflow: hidden; padding: 0; }
.form-pane { min-width: 0; height: 100%; display: flex; flex-direction: column; gap: 16px; overflow-y: auto; }
.section { display: flex; flex-direction: column; gap: 16px; padding: 22px; min-width: 0; }
.section-heading { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.section-title { font-size: 18px; font-weight: 650; }
.status-tag { color: var(--warning); font-size: 13px; font-weight: 700; }
.header-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 18px 24px; }
.header-grid > view { display: flex; flex-direction: column; gap: 6px; min-width: 0; overflow-wrap: anywhere; }
.label { color: var(--muted); font-size: 13px; }
.edit-form { display: flex; flex-direction: column; gap: 16px; }
.picker-text { display: flex; align-items: center; }
.field-error { color: var(--error); font-size: 13px; }
.history-note { padding: 12px; border-radius: 6px; background: #fff5e9; color: #704007; font-size: 13px; line-height: 1.5; }
.form-actions { display: flex; gap: 10px; flex-wrap: wrap; }
.check-line:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
.evidence-toggle { align-self: flex-start; padding-left: 0; }
.header-evidence { display: flex; flex-direction: column; gap: 10px; padding-top: 4px; }
.rows { border-top: 1px solid var(--line); }
.counts-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.row { display: flex; justify-content: space-between; gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--line); font-size: 14px; }
.row text:first-child { min-width: 0; overflow-wrap: anywhere; }
.row text:last-child { flex: none; color: var(--muted); }
.more-actions { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; }
.more-menu { position: absolute; top: calc(100% + 4px); right: 0; z-index: 8; width: min(560px, 100%); box-shadow: 0 10px 28px #173b2e20; }
.file-delete-list { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }
.file-delete-list .ui-button { max-width: 100%; overflow-wrap: anywhere; text-align: left; }
.line-row { display: flex; justify-content: space-between; align-items: center; gap: 16px; width: 100%; min-height: 48px; margin: 0; padding: 12px 8px; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; background: transparent; font-size: 14px; text-align: left; cursor: pointer; }
.line-row::after { border: 0; }
.line-row.active { background: #eef4ec; font-weight: 650; }
.line-row text:first-child { min-width: 0; overflow-wrap: anywhere; }
.line-row text:last-child { flex: none; color: var(--muted); font-weight: 400; }
.hint { font-size: 13px; }
.summary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px 22px; }
.summary-grid > view { display: flex; flex-direction: column; gap: 5px; min-width: 0; overflow-wrap: anywhere; }
.category-breakdown { display: flex; flex-direction: column; gap: 10px; min-width: 0; padding-top: 14px; border-top: 1px solid var(--line); }
.category-list { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.category-row, .adjustment-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: baseline; gap: 8px 14px; min-width: 0; padding: 9px 12px; border-radius: 6px; background: var(--surface); }
.category-name { min-width: 0; overflow-wrap: anywhere; font-weight: 650; }
.category-name-en, .category-count, .category-note { color: var(--muted); font-size: 13px; font-weight: 400; }
.category-amount { text-align: right; font-weight: 700; font-variant-numeric: tabular-nums; }
.adjustment-row { border: 1px dashed var(--line); background: transparent; }
.category-note { line-height: 1.5; overflow-wrap: anywhere; }
.warning-note { padding: 12px; border-radius: 6px; background: #fff5e9; color: #704007; line-height: 1.5; }
.blocked-reason { font-size: 14px; }
.closeout-panel { display: flex; flex-direction: column; gap: 14px; padding: 16px; border: 1px solid var(--line); border-radius: 8px; background: #f7faf6; }
.check-line { display: flex; align-items: center; gap: 8px; min-height: 44px; cursor: pointer; }
/* Narrow: one pane at a time, both kept mounted (see the component comment). */
.detail-layout.narrow .review-grid { display: block; height: auto; }
/* ⚠️ The narrow pane keeps a bounded height on purpose. With `height: auto` the
   original scrolls the *page* instead of the pane, and the page scroll is shared
   with the form — so coming back from 填写 would drop the user wherever the form
   had scrolled to, losing the spot on the paper they were reading. */
.detail-layout.narrow .document-pane { height: max(340px, calc(100vh - 250px)); }
.detail-layout.narrow .form-pane { height: auto; overflow: visible; gap: 12px; }
@media (max-width: 899px) {
  .detail-layout.narrow .back-link { display: none; }
  .detail-heading { align-items: start; }
  .detail-heading .ui-button { max-width: 135px; padding: 8px; font-size: 12px; }
  .page-title { font-size: 23px; }
  .section { padding: 16px; }
  .header-grid { grid-template-columns: 1fr; gap: 14px; }
  .summary-grid { grid-template-columns: 1fr; }
  .category-row, .adjustment-row { grid-template-columns: minmax(0, 1fr) auto; }
  .category-amount { grid-column: 2; }
  .category-count { grid-column: 1; grid-row: 2; }
}
</style>
