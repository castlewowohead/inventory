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
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  addInvoiceLine, ApiError, completeInvoice, deleteInvoiceFile, errorMessage, getInvoice,
  getInvoiceFileContent, getInvoices,
  getIntakeItemOriginal, reassignIntakeReviewContents,
  startExtraction, voidInvoice,
  type ExtractionJob, type Invoice, type InvoiceLine
} from '../../shared/api'
import { formatBytes } from '../../shared/format'
import { downloadAttachment } from '../../shared/platform/download'
import { isNarrowViewport, watchViewport } from '../../shared/platform/viewport'
import { readLineEvidence } from './evidence'
import ExtractionStatus from './ExtractionStatus.vue'
import CompletedInvoiceView from './CompletedInvoiceView.vue'
import InvoiceUpload from './InvoiceUpload.vue'
import LineEditor from './LineEditor.vue'
import InvoiceHeaderEditor from './InvoiceHeaderEditor.vue'
import LineTable from './LineTable.vue'
import OriginalDocumentViewer from './OriginalDocumentViewer.vue'

const props = defineProps<{ invoiceId: number | null }>()
const emit = defineEmits<{ back: [batchId: number | null]; saved: []; completed: [batchId: number | null]; openExisting: [id: number] }>()
/**
 * 这张单据的那几页来自哪一批；手工上传的单据没有出处，返回 null。
 *
 * 填完之后该回哪里，由**这张单据自己**说了算：只看「从哪进来的」那个内存状态，
 * 刷新一下就丢了（第 22 节）。
 */
function sourceBatchId(item: Invoice | null): number | null {
  return item?.intake_sources[0]?.batch_id ?? null
}
function returnFromInvoice() { emit('back', sourceBatchId(invoice.value)) }
const invoice = ref<Invoice | null>(null)
const state = ref<'loading' | 'ready' | 'error'>('loading')
const failure = ref('')
const saving = ref(false)
const appending = ref(false)
const narrow = ref(isNarrowViewport())
const panel = ref<'document' | 'form'>('form')
const selectedLineId = ref<number | null>(null)
/** Page the user asked the viewer to show, overriding the line's own page. */
const pageOverride = ref<number | null>(null)
/** 原件清单与识别诊断保留折叠；单据信息始终展开。 */
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
const headerEditor = ref<{ hasUnsavedChanges: () => boolean; savePending: () => Promise<boolean> } | null>(null)
const addingLine = ref(false)
const addLineError = ref('')
const closeoutOpen = ref(false)
const checkedOriginal = ref(false)
const completing = ref(false)
const closeoutError = ref('')
const closeoutSnapshot = ref('')
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
/**
 * 「识别进度」这张卡什么时候不该出现（第 17 节 ③）。
 *
 * 批次来的单据没有自己的 `files`——原件挂在 `intake_sources` 上，识别在整理页就已经
 * 做完了，这张单据上**永远不会有**识别任务。于是这张卡只会写一句「请先上传可识别
 * 的单据文件。」，而用户面前明明摆着原件和识别出来的行。它占掉的是表格和原件的
 * 位置，却一个问题也回答不了。
 *
 * ⚠️ 只有 `none`（既没有任务、也没有可识别的文件）才藏。真的有任务在跑、失败或
 * 带着警告时照常显示——那几种状态要么需要等，要么需要人动手。
 */
const extractionSilent = computed(() => {
  const item = invoice.value
  return !!item && item.files.length === 0 && item.intake_sources.length > 0
    && extractionActivity.value === 'none'
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
const canManage = computed(() => invoice.value?.status !== 'completed' && invoice.value?.status !== 'voided')
const canComplete = computed(() => {
  const item = invoice.value
  return !!item && item.status === 'ready_to_complete'
    && item.reconciliation.line_count > 0 && item.reconciliation.pending_count === 0
    && !!item.invoice_date
})
/** 「完成单据」为什么还是灰的。没有理由就是能点了。 */
const blockedReason = computed(() => {
  const item = invoice.value
  if (!item || canComplete.value) return ''
  const pending = item.reconciliation.pending_count
  if (pending > 0) return `还有 ${pending} 行待核对${item.invoice_date ? '' : '；请填写单据日期'}`
  if (!item.invoice_date) return '请填写单据日期'
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

const lines = computed<InvoiceLine[]>(() => invoice.value?.lines ?? [])
const selectedLine = computed<InvoiceLine | null>(() =>
  lines.value.find((line) => line.id === selectedLineId.value) ?? null)

/** Where the viewer should look: the selected line's evidence, unless the user moved. */
const viewerFocus = computed(() => {
  const line = selectedLine.value
  if (!line) return pageOverride.value === null ? null : { fileId: null, page: pageOverride.value }
  const evidence = readLineEvidence(line)
  return { fileId: evidence.fileId ?? (evidence.intakeItemId === null ? null : -evidence.intakeItemId), page: pageOverride.value ?? evidence.page }
})
const documentVisible = computed(() => !narrow.value || panel.value === 'document')
const formVisible = computed(() => !narrow.value || panel.value === 'form')

const headerDirty = computed(() => headerEditor.value?.hasUnsavedChanges() ?? false)
async function saveHeaderBeforeConfirm(): Promise<boolean> {
  const saved = await headerEditor.value?.savePending() ?? true
  if (saved) await nextTick()
  return saved
}
onUnmounted(() => {
  stopViewport()
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

/** Files added from the detail view: re-read so page order and counts are the server's. */
async function filesStored() {
  await load()
  emit('saved')
}

// --- 拆分 / 合并：填表期的单据边界修正 --------------------------------------
//
// 单据边界只认纸面单号，读不出单号就一页一张（docs/multifiles.md 第 12 节）。
// 那条规则宁可多拆：合错了两张发票的金额会相加，而「各行之和对不上单据总额」
// 这个检查正好因此失效；拆错了在这里一眼看得见，并回去就是。
// 没有这个入口，一张读不出单号的两页发票就再也合不回一张。

/** 选中的整理内容（`intake_sources` 的 `item_id`）。 */
const selectedSources = ref<number[]>([])
const mergeTargetId = ref<number | null>(null)
const mergeCandidates = ref<Invoice[]>([])
const boundaryBusy = ref(false)
const boundaryError = ref('')
const boundaryNote = ref('')

const intakeBatchId = computed(() => invoice.value?.intake_sources[0]?.batch_id ?? null)
/** 已经入账的行不能靠挪页改归属，那条路是撤销确认。 */
const canReassign = computed(() => !!invoice.value && canManage.value
  && invoice.value.intake_sources.length > 0
  && invoice.value.reconciliation.confirmed_count === 0)
const canSplit = computed(() => canReassign.value && selectedSources.value.length > 0
  && selectedSources.value.length < (invoice.value?.intake_sources.length ?? 0))

function toggleSource(itemId: number) {
  selectedSources.value = selectedSources.value.includes(itemId)
    ? selectedSources.value.filter((value) => value !== itemId)
    : [...selectedSources.value, itemId]
}

function sourceEntries(itemIds: number[]) {
  return (invoice.value?.intake_sources ?? [])
    .filter((source) => itemIds.includes(source.item_id))
    .map((source) => ({ id: source.item_id, version: source.version }))
}

/**
 * 能并进去的其它草稿：同一批次、同一天、同一商家、还没入账过。
 *
 * 这四个条件是服务端自己的规则，先在界面上筛一遍，免得让人选中一个必然被拒的
 * 目标。真正的判定仍在服务端，被拒的理由原样显示出来。
 */
async function loadMergeCandidates() {
  const item = invoice.value
  mergeCandidates.value = []
  mergeTargetId.value = null
  if (!item || !item.invoice_date || !item.source_name || intakeBatchId.value === null) return
  const page = await getInvoices(`?completed=false&limit=50`
    + `&date_from=${item.invoice_date}&date_to=${item.invoice_date}`
    + `&source=${encodeURIComponent(item.source_name)}`)
  mergeCandidates.value = page.items.filter((row) => row.id !== item.id
    && row.entry_source === item.entry_source
    && row.reconciliation.confirmed_count === 0
    && row.intake_sources.some((source) => source.batch_id === intakeBatchId.value))
}

async function runReassign(itemIds: number[], targetInvoiceId?: number) {
  const item = invoice.value
  if (!item || intakeBatchId.value === null || boundaryBusy.value) return
  boundaryBusy.value = true
  boundaryError.value = ''
  boundaryNote.value = ''
  try {
    const result = await reassignIntakeReviewContents(
      intakeBatchId.value, item.id, sourceEntries(itemIds), targetInvoiceId
    )
    selectedSources.value = []
    if (result.source_invoice_id === item.id && itemIds.length === item.intake_sources.length) {
      // 整张并走之后这张草稿已经作废，留在这里只会显示一张空单据。
      emit('saved')
      emit('openExisting', result.target_invoice_id)
      return
    }
    boundaryNote.value = result.created
      ? `已拆出单据 #${result.target_invoice_id}。两张的单据总额都已清空，请各自照纸面填。`
      : `已并入单据 #${result.target_invoice_id}。单据总额已清空，请照纸面填。`
    emit('saved')
    await refreshQuiet()
    await loadMergeCandidates()
  } catch (error) {
    boundaryError.value = errorMessage(error)
  } finally {
    boundaryBusy.value = false
  }
}

/** 展开原件清单时才去查可并入的草稿：收着的时候没人需要这份名单。 */
function toggleFiles() {
  filesOpen.value = !filesOpen.value
  boundaryError.value = ''
  boundaryNote.value = ''
  selectedSources.value = []
  if (filesOpen.value && canReassign.value) void loadMergeCandidates()
}

async function splitSelected() {
  if (!canSplit.value) return
  if (!await confirmAction('拆成另一张单据',
    '选中的页会变成一张新的草稿。两张的单据总额都会清空，需要各自照纸面重新填。')) return
  await runReassign(selectedSources.value)
}

async function mergeIntoTarget() {
  const item = invoice.value
  if (!item || mergeTargetId.value === null) return
  if (!await confirmAction('并入另一张单据',
    `这张草稿的 ${item.intake_sources.length} 页会全部并入单据 #${mergeTargetId.value}，`
    + '本草稿随即作废。单据总额会清空，需要照纸面重新填。')) return
  await runReassign(item.intake_sources.map((source) => source.item_id), mergeTargetId.value)
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
  if (!await confirmAction('重新识别', '重新识别会更新尚未修改的内容。你已经修改的行会保留，但仍需核对；如果已有明细入账，请先处理这些明细。')) return
  actionBusy.value = true
  extractionSettling.value = true
  originalOnly.value = false
  manualAfterFailure.value = false
  try {
    await startExtraction(id)
    actionNote.value = '已开始重新识别。识别完成后，请逐行核对。'
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
  if (!await confirmAction('删除原件', `确定删除「${name}」？删除后无法查看这份文件，请先确认单据还有其他原件。`)) return
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

/**
 * 删除本张单据：清空数据，并把它从整理页上彻底移除（第 17 节 ④）。
 *
 * ⚠️ 问的那句话照用户 2026-11-24 的原话写。批次来的单据，服务端连它在整理页上的
 * 那几页内容一起真删——所以这里说得出「整理页上也不会再有它」，而不是让用户回到
 * 整理页才发现几张「草稿已作废」的卡片还杵在那里。删完就**离开这一页**：单据没
 * 了，留在它的详情页上只能看到一个空壳。
 */
async function cancelInvoice() {
  if (!await prepareAction() || !invoice.value) return
  const id = invoice.value.id
  const batchId = sourceBatchId(invoice.value)
  const onBoard = invoice.value.intake_sources.length
  if (!await confirmAction('删除本张单据', onBoard
    ? `是否清空数据，并彻底移除本张单据？这张单据填过的内容会清空，它在整理页上的 ${onBoard} 项内容也会一并移除；重新上传同一份文件会当作全新的一份。`
    : '是否清空数据，并彻底移除本张单据？这张单据和它的原件都会删掉，同一个文件之后可以重新上传。')) return
  const bookedIds = invoice.value.lines.filter((line) => line.status === 'confirmed').map((line) => line.id)
  if (bookedIds.length && !await confirmAction(
    '确认移除已入账数据',
    `这张单据有 ${bookedIds.length} 行已经入账。确认删除后，这些采购记录会作废，不再计入价格统计；原件也会删除。是否确认移除？`
  )) return
  actionBusy.value = true
  let removed = false
  try {
    const result = await voidInvoice(id, bookedIds.length ? bookedIds : undefined)
    invoice.value = result.invoice
    if (result.status !== 'voided') throw new Error('暂时无法确认删除结果，请重新读取单据。')
    actionNote.value = '单据已删除。'
    removed = true
    emit('saved')
  } catch (error) {
    actionError.value = `${errorMessage(error)} 请根据最新状态处理后再试。`
  } finally {
    // 删成功就别再去读它了：那几页内容已经不在整理页上，详情也只剩一个空壳。
    if (!removed) await syncAfterAction()
    actionBusy.value = false
  }
  // 回到来的地方（整理页或单据库）。
  if (removed) emit('back', batchId)
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
    files: [...item.files.map((file) => [file.id, file.page_order]),
      ...item.intake_sources.map((source) => [source.item_id, source.page_no ?? source.row_no])],
    lines: item.lines.map((line) => [line.id, line.status, line.version]),
    header: [item.invoice_date, item.invoice_total, item.currency, item.header_reviewed_fields],
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
    if (item.status === 'completed') { emit('completed', sourceBatchId(item)); return }
    if (!canComplete.value) {
      locatePending(item)
      closeoutError.value = item.reconciliation.pending_count > 0
        ? `还有 ${item.reconciliation.pending_count} 行待核对，请逐行确认或跳过。`
        : !item.invoice_date ? '请先填写单据日期。' : '单据尚不能完成；请先添加并处理至少一行。'
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
    if (latest.status === 'completed') { emit('completed', sourceBatchId(latest)); return }
    if (!canComplete.value) {
      closeoutOpen.value = false
      checkedOriginal.value = false
      locatePending(latest)
      closeoutError.value = latest.invoice_date ? '单据状态已变化，请处理待核对行后重新检查收口摘要。' : '请先填写单据日期。'
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
      throw new Error('暂时无法确认单据是否完成，请重新读取单据。')
    }
    emit('completed', sourceBatchId(outcome.invoice))
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
        if (latest.status === 'completed') emit('completed', sourceBatchId(latest))
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

async function downloadIntakeOriginal(batchId: number, itemId: number, filename: string) {
  originalDownloadError.value = ''
  try {
    downloadAttachment(await getIntakeItemOriginal(batchId, itemId), filename)
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
  appending.value = false
  selectedLineId.value = null
  pageOverride.value = null
  panel.value = 'form'
  closeoutOpen.value = false
  filesOpen.value = false
  diagnosticsOpen.value = false
  checkedOriginal.value = false
  closeoutError.value = ''
  actionError.value = ''
  actionNote.value = ''
  void load()
}, { immediate: true })
</script>

<template>
  <CompletedInvoiceView v-if="invoice && state === 'ready' && invoice.status === 'completed'" :invoice="invoice" @back="returnFromInvoice" @open-existing="emit('openExisting', $event)" @currency-changed="applyStoredInvoice($event, false)" @reopened="applyStoredInvoice($event, false)" />
  <view v-else class="detail-layout" :class="{ narrow }">
    <view class="detail-heading">
      <view class="heading-primary"><button class="back-link" @click="returnFromInvoice">← {{ sourceBatchId(invoice) !== null ? '返回分类页面' : '返回单据' }}</button><text class="page-title">单据{{ invoice ? ` #${invoice.id}` : '' }}</text></view>
      <!-- 右上角只有这一个动作（第 17 节 ④）。原来是一个「单据更多操作」菜单，
           里面混着重新提取、识别记录和删除拍坏文件——那三样都是对着原件做的事，
           已经搬到下面「原件文件」那一折里，这里不再留第二个入口。 -->
      <view v-if="invoice && canManage && !extractionGate" class="more-actions">
        <button class="ui-button ghost delete-invoice" :disabled="actionBusy" @click="cancelInvoice">删除本张单据</button>
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
        :hidden="extractionGate !== null || extractionSilent || (extractionActivity === 'done' && !diagnosticsOpen)"
        @finished="extractionFinished"
        @activity="extractionActivity = $event"
        @jobs="extractionJobs = $event"
      />
      <view v-if="extractionGate" class="extraction-screen">
        <template v-if="originalOnly && (extractionGate === 'busy' || extractionGate === 'failed')">
          <view class="escape-actions"><button class="ui-button secondary" @click="originalOnly = false">返回识别状态</button></view>
          <view class="ui-card original-only"><OriginalDocumentViewer :invoice-id="invoice.id" :files="invoice.files" :sources="invoice.intake_sources" :is-fake="invoice.recognition.is_fake" :focus="null" :visible="true" /></view>
        </template>
        <view v-else-if="extractionGate === 'busy'" class="extraction-wait" role="status" aria-live="polite">
          <view class="wait-indicator" aria-hidden="true" />
          <text class="wait-title">{{ waitingText }}</text>
          <text class="muted">{{ invoice.source_name || '来源待填' }} · {{ invoice.files.length + invoice.intake_sources.length }} 个原件内容</text>
          <view v-if="waitingLong" class="wait-escape">
            <text>先去忙别的，识别完会留在待办里。</text>
            <view class="escape-actions">
              <button class="ui-button secondary" @click="returnFromInvoice">{{ sourceBatchId(invoice) !== null ? '返回分类页面' : '返回单据列表' }}</button>
              <button class="ui-button secondary" @click="originalOnly = true">只看原件</button>
              <button class="ui-button ghost" @click="extractionStatus?.refresh()">刷新识别状态</button>
            </view>
          </view>
        </view>
        <view v-else-if="extractionGate === 'failed'" class="extraction-wait failed-screen" role="alert">
          <text class="wait-title">原件识别失败</text>
          <text>请重试识别；如果仍然失败，可以对照原件手工录入。</text>
          <text class="muted">{{ invoice.source_name || '来源待填' }} · {{ invoice.files.length + invoice.intake_sources.length }} 个原件内容</text>
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
        <text>有原件没有识别完成。请核对已有明细与原件。</text>
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
            <button v-for="source in invoice.intake_sources" :key="source.item_id" class="ui-button secondary" @click="downloadIntakeOriginal(source.batch_id, source.item_id, source.original_name)">下载 {{ source.original_name }} · 第 {{ source.row_no }} 行</button>
            <text v-if="originalDownloadError" class="field-error" role="alert">{{ originalDownloadError }}</text>
          </view>
          <OriginalDocumentViewer
            v-else
            :invoice-id="invoice.id"
            :files="invoice.files"
            :sources="invoice.intake_sources"
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
                :before-confirm="saveHeaderBeforeConfirm"
                @applied="applyLine"
                @stored="applyStoredInvoice"
                @reload="refreshQuiet"
              />
            </template>
          </LineTable>
          <text v-if="addLineError" class="field-error" role="alert">{{ addLineError }}</text>

          <!-- 收口：对账数字只在真要完成时出现，平时不占地方。 -->
          <InvoiceHeaderEditor ref="headerEditor" :invoice="invoice" :can-manage="canManage" @stored="applyStoredInvoice($event, false)" />

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
                  <view><text class="label">原件内容</text><text>{{ invoice.files.length + invoice.intake_sources.length }} 个</text></view>
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
                <!--
                  ⚠️ 这一行曾经是 `<label>` 包着一个 `<checkbox>`，而那是个**哑火的勾**
                  （2026-11-24 反馈，第 20 节 ①）：uni-app 的 `label` 在点击目标不是
                  勾选框本身时，直接调勾选框组件内部的 `_onClick`，**不派发 DOM click**。
                  于是点这行文字＝勾看上去打上了（组件自己的内部状态）、`checkedOriginal`
                  仍是 false、「完成单据」继续灰着，而且没有任何解释。
                  现在整行就是一个按钮，勾的样子由 `checkedOriginal` 一处决定，没有第二
                  份状态可以和它打架。方框的尺寸沿用整理页那一个（第 13 节 ④）。
                -->
                <button class="check-line" role="checkbox" :aria-checked="checkedOriginal"
                  @click="checkedOriginal = !checkedOriginal">
                  <text class="check-box">{{ checkedOriginal ? '✓' : '' }}</text><text>我已检查原件有无漏行</text>
                </button>
                <view class="form-actions"><button class="ui-button" :disabled="!checkedOriginal || completing" @click="finishInvoice">{{ completing ? '正在完成…' : '完成单据' }}</button><button class="ui-button secondary" :disabled="completing" @click="closeoutOpen = false; checkedOriginal = false">取消</button></view>
              </view>
            </template>
            <text v-if="closeoutError" class="field-error" role="alert">{{ closeoutError }}</text>
          </view>

          <!-- 原件文件清单与补传：也折起来。原件本身在左边一直看得见。 -->
          <view class="ui-card section fold">
            <button class="fold-toggle" :aria-expanded="filesOpen" @click="toggleFiles">
              {{ filesOpen ? '收起原件文件' : '原件文件' }}
              <text class="fold-summary">{{ invoice.files.length + invoice.intake_sources.length }} 个</text>
            </button>
            <template v-if="filesOpen">
              <view v-if="invoice.files.length" class="rows">
                <view v-for="file in invoice.files" :key="file.id" class="row">
                  <text>第 {{ file.page_order }} 页 · {{ file.original_name }}</text>
                  <text>{{ formatBytes(file.size) }}</text>
                  <button v-if="invoice.entry_source === 'import'" class="ui-button secondary" @click="downloadOriginal(file.id, file.original_name)">下载原始表格</button>
                </view>
              </view>
              <view v-if="invoice.intake_sources.length" class="rows">
                <view v-for="source in invoice.intake_sources" :key="source.item_id" class="row">
                  <button v-if="canReassign" class="source-pick"
                    :aria-pressed="selectedSources.includes(source.item_id) ? 'true' : 'false'"
                    :aria-label="`${selectedSources.includes(source.item_id) ? '取消选中' : '选中'} ${source.original_name} ${source.kind === 'page' ? `第 ${source.page_no} 页` : `第 ${source.row_no} 行`}`"
                    @click="toggleSource(source.item_id)">
                    <text class="tick">{{ selectedSources.includes(source.item_id) ? '✓' : '' }}</text>
                    <text>{{ source.original_name }} · {{ source.kind === 'page' ? `第 ${source.page_no} 页` : `第 ${source.row_no} 行` }}</text>
                  </button>
                  <text v-else>{{ source.original_name }} · {{ source.kind === 'page' ? `第 ${source.page_no} 页` : `第 ${source.row_no} 行` }}</text>
                  <button class="ui-button secondary" @click="downloadIntakeOriginal(source.batch_id, source.item_id, source.original_name)">下载原件</button>
                </view>
              </view>

              <!--
                单据边界的修正入口。读不出单号时一页一张是默认值（第 12 节），
                所以「这两页其实是一张」必须有一条合回去的路。
              -->
              <view v-if="canReassign" class="boundary">
                <text class="muted boundary-note">这张单据包含上面这些页。它们不是一张发票时，在这里拆开或并到另一张。</text>
                <view class="boundary-actions">
                  <button class="ui-button secondary" :disabled="!canSplit || boundaryBusy" @click="splitSelected">
                    拆出选中的 {{ selectedSources.length }} 页
                  </button>
                  <picker
                    v-if="mergeCandidates.length"
                    :range="mergeCandidates.map((row) => `单据 #${row.id}${row.invoice_number ? ' · 单号 ' + row.invoice_number : ' · 无单号'}`)"
                    :value="mergeCandidates.findIndex((row) => row.id === mergeTargetId)"
                    @change="mergeTargetId = mergeCandidates[Number($event.detail.value)]?.id ?? null"
                  >
                    <view class="picker-face">{{ mergeTargetId === null ? '选择要并入的单据' : `并入单据 #${mergeTargetId}` }}</view>
                  </picker>
                  <button class="ui-button secondary" :disabled="mergeTargetId === null || boundaryBusy" @click="mergeIntoTarget">
                    整张并过去
                  </button>
                </view>
                <text v-if="!mergeCandidates.length" class="muted boundary-note">同一天、同一商家没有别的待填草稿，所以只能拆、不能并。</text>
                <text v-if="boundaryNote" class="muted boundary-note">{{ boundaryNote }}</text>
                <text v-if="boundaryError" class="field-error" role="alert">{{ boundaryError }}</text>
              </view>
              <text v-if="!invoice.files.length && !invoice.intake_sources.length" class="muted">暂无原件文件。</text>
              <text v-if="originalDownloadError" class="field-error" role="alert">{{ originalDownloadError }}</text>
              <button v-if="canManage" class="ui-button secondary" :aria-pressed="appending" @click="appending = !appending">{{ appending ? '收起补传' : '补传文件' }}</button>
              <!-- 原「单据更多操作」菜单里的三样，都是对着这些原件做的事，所以住在这里。 -->
              <view v-if="canManage" class="form-actions">
                <button v-if="invoice.entry_source !== 'import' && !invoice.intake_sources.length" class="ui-button secondary" :disabled="actionBusy" @click="retryExtraction">重新提取 / 失败重试</button>
                <!-- 批次来的单据在这一页上没有识别任务，这个按钮只会打开一张空卡。 -->
                <button v-if="invoice.entry_source !== 'import' && !extractionSilent" class="ui-button secondary" :aria-pressed="diagnosticsOpen" @click="diagnosticsOpen = !diagnosticsOpen">{{ diagnosticsOpen ? '收起识别记录' : '查看识别记录' }}</button>
              </view>
              <view v-if="canManage && invoice.files.length" class="file-delete-list">
                <text class="muted">删除拍坏文件</text>
                <button v-for="file in invoice.files" :key="file.id" class="ui-button ghost" :disabled="actionBusy" @click="removeFile(file.id, file.original_name)">删除第 {{ file.page_order }} 页 · {{ file.original_name }}</button>
              </view>
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
.source-pick { min-width: 0; min-height: 44px; margin: 0; padding: 4px 0; display: flex; align-items: center; gap: 10px; border: 0; background: transparent; color: inherit; font-size: 14px; text-align: left; cursor: pointer; }
.source-pick::after { border: 0; }
.tick { flex: none; width: 22px; height: 22px; display: flex; align-items: center; justify-content: center; border: 1px solid var(--line); border-radius: 5px; color: var(--green); font-size: 14px; font-weight: 700; }
.source-pick[aria-pressed="true"] .tick { border-color: var(--green); background: #eef5ef; }
.boundary { display: flex; flex-direction: column; gap: 10px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--line); }
.boundary-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.boundary-note { font-size: 13px; line-height: 1.6; }
.picker-face { min-height: 44px; display: flex; align-items: center; padding: 8px 14px; border: 1px solid var(--line); border-radius: 8px; font-size: 14px; }
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
/* 红字、不是红底：它真的会清空这张单据（第 13 节 ② 的口径），但它独自待在标题行
   右上角，一整块红在那里比按钮本身更吵。确认仍在对话框里问一次。 */
.delete-invoice { color: var(--error); }
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
.check-line { display: flex; align-items: center; gap: 10px; min-height: 44px; margin: 0; padding: 0 4px 0 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.check-line::after { border: 0; }
/* 点击区是整行（≥44px 高），看得见的方框 22px——与整理页的勾选框同一个样子。 */
.check-box { width: 22px; height: 22px; flex: none; display: flex; align-items: center; justify-content: center; border: 2px solid #8aa394; border-radius: 4px; background: #fff; color: transparent; font-size: 15px; font-weight: 800; line-height: 1; }
.check-line[aria-checked="true"] .check-box { border-color: var(--green); background: var(--green); color: #fff; }
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
