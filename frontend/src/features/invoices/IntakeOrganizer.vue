<script setup lang="ts">
/**
 * 按日期和商家整理（M10）：左边待归类卡片，右边「日期 → 商家」目标组。
 *
 * ### Three ways to move, one command
 *
 * Dragging a grip, pressing 移入所选 on a group, and choosing a target in the
 * 移到… panel all end up in `dispatchMove`, which posts the single M06 command
 * (`POST /api/intake-batches/<id>/moves`). That is the plan's requirement
 * (section 4) and also the only way the three paths cannot drift apart: the
 * version check, the idempotency key and the undo journal are the server's,
 * not three client-side variations of them.
 *
 * ### Handing a suggested or edited group to review
 *
 * 整理 ends here; 填写 and 逐行确认 do not live on this page. The 送入审核 button
 * posts the M08 command for the **explicitly selected** contents of one
 * group — never for "the group", because a 日期/商家 group may hold
 * several real invoices and a draft made of all of them would be a fake single
 * invoice. A handed-off content stops accepting moves; the card says so instead
 * of waiting for the server's 409.
 *
 * Dragging is the desktop shortcut only. Every move is reachable by keyboard
 * and by single taps, which is what makes the narrow layout and W3C G219 work.
 *
 * ### What the board is, and is not
 *
 * The right-hand side is a read-only projection of paper candidates plus human
 * edits. A same-file unambiguous date can disambiguate the other pages. Group
 * labels show the suggestion count; nothing is written as a human decision
 * until the person edits the group or sends selected contents to review.
 *
 * ### Conflicts and failures
 *
 * Contents carry a version. The whole batch of a move is checked before the
 * first write, so a stale card fails the move instead of half-applying it; the
 * page then says so and re-reads. A failure that is not a conflict keeps the
 * same `request_key` for 重试这一步, so a retry cannot move anything twice.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  ApiError, closeIntakeBatch, createIntakeDateGroup, deleteIntakeDateGroup, deleteIntakeItem, errorMessage, getIntakeBatch, getIntakeGroupItems, getIntakeGroups, getInvoice, voidInvoice,
  getCurrentIntakeBatch, getIntakeItems, moveIntakeItems, processIntakeBatch, startIntakeForms,
  recognizeIntakeBatch,
  refillIntakeForm, undoIntakeMove, withdrawIntakeForms,
  type IntakeBatch, type IntakeGroup, type IntakeItem, type IntakeSource, type Invoice
} from '../../shared/api'
import { isNarrowViewport, watchViewport } from '../../shared/platform/viewport'
import { boardDragSupported, cancelBoardDrag, startBoardDrag } from '../../shared/platform/dragBoard'
import DateFieldInput from './DateFieldInput.vue'
import IntakeCard from './IntakeCard.vue'
import OriginalDocumentViewer from './OriginalDocumentViewer.vue'
import {
  alreadyThere, bucketKey, buildBoard, cardLocation, dropKey, handoffProblem, isHandedOff,
  isIsoDate, knownSourceOf, monthDayLabel, paperSourceOf, parseDropKey, recognitionNote,
  sameSource, suggestionsOf, yearLabel,
  zoneAction, zoneLabel, zoneTarget,
  type BoardTarget, type DropZone
} from './intakeBoard'

const props = withDefaults(defineProps<{ batchId?: number | null; active?: boolean }>(), { batchId: null, active: true })
const emit = defineEmits<{
  upload: []; ledger: []; opened: [id: number]; review: [invoiceIds: number[]]; finished: []
}>()

/** How many cards one page of a bucket holds. 加载更多 asks for the next page. */
const PAGE_SIZE = 20
/** Show individual cards immediately when the board is small enough to scan. */
const AUTO_OPEN_MAX_GROUPS = 12
const AUTO_OPEN_MAX_ITEMS = 30
/** Groups per request; a batch with more than this many groups keeps paging. */
const GROUP_PAGE = 100
const MAX_GROUPS = 300

interface Bucket {
  items: IntakeItem[]
  total: number
  loading: boolean
  error: string
}

const PENDING: BoardTarget = { purchaseDate: null, sourceName: null }
const PENDING_ZONE: DropZone = { kind: 'pending' }

const batch = ref<IntakeBatch | null>(null)
/** Older running backends do not yet send the two completion counters. */
const fallbackCounts = ref<{ batchId: number; total: number; unfinished: number } | null>(null)
const groups = ref<IntakeGroup[]>([])
const buckets = ref<Record<string, Bucket>>({})
const expanded = ref<string[]>([])
const manuallyClosed = new Set<string>()
const selection = ref<IntakeItem[]>([])
const loading = ref(true)
const error = ref('')
const conflict = ref('')
const notice = ref('')
const moving = ref(false)
const processing = ref(false)
const recognizing = ref(false)
const lastMove = ref<{ id: number; text: string } | null>(null)
const failed = ref<{ key: string; zone: DropZone; items: IntakeItem[] } | null>(null)
const preview = ref<IntakeItem | null>(null)
const cardMenu = ref<IntakeItem | null>(null)
const deleteAsk = ref<IntakeItem | null>(null)
const deleteInvoice = ref<Invoice | null>(null)
const deleteLoading = ref(false)
const deleting = ref(false)
const deleteError = ref('')
interface MovePanel {
  items: IntakeItem[]
  /** `pick` uses a date already on the board, `type` the typed one. */
  dateMode: 'pick' | 'type'
  date: string
  typedDate: string
  /** `unknown` = 商家待定: the move then writes only the date. */
  sourceMode: 'unknown' | 'pick' | 'new'
  source: string
  typedSource: string
}
const panel = ref<MovePanel | null>(null)
/**
 * 改日期/改商家的那张小面板。`single` 为真时改的是**一张卡片**（第 17 节 ①），
 * `choices` 是可以直接点的商家名：这张纸自己读出的，加上这一批里已经有的。
 */
const groupEdit = ref<{
  kind: 'date' | 'source'; items: IntakeItem[]; value: string
  single: boolean; choices: string[]
} | null>(null)
const loadingGroupEdit = ref(false)
/**
 * 「这一张要放进别人家吗」——拖到某个商家组、而这张卡片自己已经有商家时问一次。
 *
 * ⚠️ 这不是多一道手续，而是一次实际发生过的静默覆盖：`03_Kunkee_Trading_1p.pdf`
 * 那一页纸面读出 KUN KEE TRADING INC.，一下拖进 NEW MOY'S 组就被写成了
 * NEW MOY'S，之后再怎么改日期都跟着错（第 17 节 ②）。两条路都留着：合并过去，
 * 或者只改日期、把它自己的商家留着。
 */
const sourceClash = ref<{ zone: DropZone; items: IntakeItem[]; target: string; names: string[] } | null>(null)
const handing = ref(false)
/** The cards a pending 撤回填表 would clear; `null` while nothing is being asked. */
const withdrawAsk = ref<IntakeItem[] | null>(null)
const withdrawing = ref(false)
/**
 * 撤回的结果，就留在刚才点的那个对话框里。
 *
 * ⚠️ 原来成功就把对话框关掉、把一句话写在**页面顶上**。整理页很长，用户点的
 * 是屏幕中段某一组里的按钮，于是「点了撤回，一点反应都没有」（2026-11-24 反馈，
 * 第 13 节 ③）。结果必须出现在手指落下的地方。
 */
const withdrawResult = ref<{ ok: boolean; text: string } | null>(null)
/**
 * 已完成的那张单据要重来一遍：问一句，然后清空它并落进新的空表（第 20 节）。
 *
 * ⚠️ 这里没有「结果对话框」——撤回填表需要一个（撤完人还站在整理页上，第 13 节 ③），
 * 而重新填写成功的反馈是**整页换成了那张空表**，再弹一句只是挡路。失败才留在原地说。
 */
const refillAsk = ref<IntakeItem | null>(null)
const refilling = ref(false)
const refillError = ref('')
/**
 * 「确认完成所有填写」正在收口（第 22 节）。
 *
 * 失败就留在按钮旁边说（同第 13 节 ③：反馈要落在手指边上）；成功的反馈是
 * 整页换成了单据库，再弹一句只是挡路。
 */
const finishing = ref(false)
const finishError = ref('')
const newDate = ref('')
const dateBusy = ref(false)
const dateToDelete = ref<string | null>(null)
const narrow = ref(isNarrowViewport())
/** 标题旁边那个「▾」展开的显示形式菜单（卡片整理 / 财务表格）。 */
const viewMenu = ref(false)
/**
 * 这一批还有几项识别失败——**只为了决定「重试识别失败项」要不要出现**。
 *
 * 不能数页面上已加载的卡片：整理页只加载展开了的那几组，一张收起来的组里的失败
 * 项数不到。所以问服务端一次 `missing=recognition_failed`，只要 `total`。
 */
const failedCount = ref(0)
const pane = ref<'pending' | 'board'>('pending')
const dragging = ref<{ count: number; x: number; y: number } | null>(null)
const hoverKey = ref<string | null>(null)
const canDrag = boardDragSupported()

let dragItems: IntakeItem[] = []
let moveCounter = 0
let stopped = false
let stopViewport = () => {}
let poll: ReturnType<typeof setInterval> | null = null

const board = computed(() => buildBoard(groups.value))
const canMove = computed(() => batch.value?.status === 'open')
/**
 * 还剩几项没填完——**问服务端的那个数**，不是数页面上的卡片。
 *
 * 整理页只加载展开了的那几组，一个收起来的组里还剩几项数不到，那样这个按钮会在
 * 真还没填完时亮起来——比少一个按钮糟得多（同第 18 节 ①）。
 */
const itemCount = computed(() => {
  if (!batch.value) return null
  if (typeof batch.value.item_count === 'number') return batch.value.item_count
  return fallbackCounts.value?.batchId === batch.value.id ? fallbackCounts.value.total : null
})
const unfinishedCount = computed(() => {
  if (!batch.value) return null
  if (typeof batch.value.unfinished_item_count === 'number') return batch.value.unfinished_item_count
  return fallbackCounts.value?.batchId === batch.value.id ? fallbackCounts.value.unfinished : null
})
/** 按不动时说得出还差什么（第 13 节 ⑥：按钮自己暗着并解释，不是点下去才被拒）。 */
const finishBlocked = computed(() => {
  if (!batch.value) return ''
  if (!canMove.value) return '这一批已经结束了。'
  if (itemCount.value === null || unfinishedCount.value === null) return '正在核对这一批的填写状态…'
  if (itemCount.value === 0) return '这一批还没有可整理的内容。'
  if (unfinishedCount.value > 0) {
    return `还有 ${unfinishedCount.value} 项没有填完：每一项都要填完并「完成单据」，或者在上传页把用不上的文件移除。`
  }
  return ''
})
const pendingBucket = computed(() => buckets.value[bucketKey(PENDING)] ?? null)
const contentTotal = computed(() => groups.value.reduce((sum, group) => sum + group.item_count, 0))
const groupedTotal = computed(() => contentTotal.value - board.value.pendingCount)

/** Read every item only when the running API lacks the completion counters. */
async function loadFallbackCounts(current: IntakeBatch) {
  if (typeof current.item_count === 'number' && typeof current.unfinished_item_count === 'number') {
    fallbackCounts.value = null
    return
  }
  fallbackCounts.value = null
  let offset = 0
  let total = 0
  let unfinished = 0
  do {
    const page = await getIntakeItems(current.id, 200, offset)
    total = page.total
    unfinished += page.items.filter((item) => item.review_status !== 'completed').length
    offset += page.items.length
    if (!page.items.length && offset < total) throw new Error('无法读取完整批次内容，请刷新后重试。')
  } while (offset < total)
  if (!stopped && batch.value?.id === current.id) fallbackCounts.value = { batchId: current.id, total, unfinished }
}
const selectedIds = computed(() => new Set(selection.value.map((item) => item.id)))
/** Every supplier name decided anywhere in this batch, for the 移到… panel. */
const knownSources = computed(() => {
  const names = new Map<string, string>()
  for (const group of groups.value) {
    if (group.source_name) names.set(group.source_name_normalized ?? group.source_name, group.source_name)
  }
  return [...names.values()]
})
const unsettledFiles = computed(() =>
  (batch.value?.files ?? []).filter((file) => file.status === 'pending' || file.status === 'failed'))

/**
 * ⚠️ Returns the bucket **read back out of the ref**, never the object that was
 * just put in. Writing to the plain object would skip the reactive proxy, and
 * the symptom is specific and misleading: the group keeps showing 正在读取…
 * forever because the finished load never reaches the template.
 */
function bucketOf(target: BoardTarget): Bucket {
  const key = bucketKey(target)
  if (!buckets.value[key]) buckets.value[key] = { items: [], total: 0, loading: false, error: '' }
  return buckets.value[key]
}

function isExpanded(target: BoardTarget): boolean {
  return expanded.value.includes(bucketKey(target))
}

async function toggleExpanded(target: BoardTarget) {
  const key = bucketKey(target)
  if (expanded.value.includes(key)) {
    manuallyClosed.add(key)
    expanded.value = expanded.value.filter((value) => value !== key)
    return
  }
  manuallyClosed.delete(key)
  expanded.value = [...expanded.value, key]
  if (!buckets.value[key]?.items.length) await loadBucket(target)
}

/** A re-read hands over fresh versions; the selection must follow them. */
function syncSelection(items: IntakeItem[]) {
  if (!selection.value.length) return
  const byId = new Map(items.map((item) => [item.id, item]))
  selection.value = selection.value.map((item) => byId.get(item.id) ?? item)
}

async function loadBucket(target: BoardTarget, append = false) {
  if (!batch.value) return
  const bucket = bucketOf(target)
  bucket.loading = true
  try {
    const page = await getIntakeGroupItems(batch.value.id, {
      purchaseDate: target.purchaseDate, sourceName: target.sourceName
    }, PAGE_SIZE, append ? bucket.items.length : 0)
    bucket.items = append ? [...bucket.items, ...page.items] : page.items
    bucket.total = page.total
    bucket.error = ''
    syncSelection(page.items)
  } catch (reason) {
    bucket.error = errorMessage(reason)
  } finally {
    bucket.loading = false
  }
}

/** 只问个数；失败了就当作「没有失败项」，不拿它去打扰工具栏。 */
async function loadFailedCount(batchId: number) {
  try {
    failedCount.value = (await getIntakeItems(batchId, 1, 0, { missing: 'recognition_failed' })).total
  } catch { failedCount.value = 0 }
}

async function loadGroups(batchId: number) {
  const collected: IntakeGroup[] = []
  for (;;) {
    const page = await getIntakeGroups(batchId, GROUP_PAGE, collected.length)
    collected.push(...page.groups)
    if (!page.groups.length || collected.length >= page.total || collected.length >= MAX_GROUPS) break
  }
  groups.value = collected
}

/**
 * 这一批填完了（第 22 节）：收口，回单据库。
 *
 * 没有确认对话框：按钮只有在这一批**一项不剩**时才按得动，收口不删任何东西、
 * 也不动已经入账的采购记录，多问一句只是挡路（红色对话框留给真会清空东西的那些
 * 操作，第 13 节 ②）。
 */
async function finishBatch() {
  if (!batch.value || finishing.value || finishBlocked.value) return
  finishing.value = true
  finishError.value = ''
  try {
    batch.value = await closeIntakeBatch(batch.value.id)
    emit('finished')
  } catch (reason) {
    finishError.value = errorMessage(reason)
    await refresh()
  } finally { finishing.value = false }
}

async function addDateGroup() {
  if (!batch.value || dateBusy.value) return
  const value = newDate.value.trim()
  if (!isIsoDate(value)) { error.value = '请填写合法的 YYYY-MM-DD 日期。'; return }
  dateBusy.value = true
  try {
    await createIntakeDateGroup(batch.value.id, value)
    newDate.value = ''
    error.value = ''
    notice.value = `已新建 ${monthDayLabel(value)} 日期类别。`
    await refresh()
  } catch (reason) { error.value = errorMessage(reason) }
  finally { dateBusy.value = false }
}

async function removeDateGroup() {
  const value = dateToDelete.value
  if (!batch.value || !value || dateBusy.value) return
  dateBusy.value = true
  try {
    await deleteIntakeDateGroup(batch.value.id, value)
    dateToDelete.value = null
    error.value = ''
    notice.value = `已删除 ${monthDayLabel(value)} 空日期类别。`
    await refresh()
  } catch (reason) { error.value = errorMessage(reason) }
  finally { dateBusy.value = false }
}

/** Small boards expose their individual cards so a card can be dragged directly. */
async function reloadVisible() {
  const live = new Set(groups.value.map((group) =>
    bucketKey({ purchaseDate: group.purchase_date, sourceName: group.source_name })))
  expanded.value = expanded.value.filter((key) => live.has(key))
  const visibleGroups = groups.value.filter((group) =>
    group.purchase_date !== null || group.source_name !== null)
  if (visibleGroups.length <= AUTO_OPEN_MAX_GROUPS
    && visibleGroups.reduce((total, group) => total + group.item_count, 0) <= AUTO_OPEN_MAX_ITEMS) {
    expanded.value = [...new Set([...expanded.value, ...visibleGroups
      .map((group) => bucketKey({ purchaseDate: group.purchase_date, sourceName: group.source_name }))
      .filter((key) => !manuallyClosed.has(key))])]
  }
  const targets: BoardTarget[] = [PENDING]
  for (const group of groups.value) {
    const target = { purchaseDate: group.purchase_date, sourceName: group.source_name }
    if (expanded.value.includes(bucketKey(target))) targets.push(target)
  }
  await Promise.all(targets.map((target) => loadBucket(target)))
}

async function refresh(showSpinner = false) {
  if (!batch.value) return
  if (showSpinner) loading.value = true
  try {
    const [current] = await Promise.all([
      getIntakeBatch(batch.value.id), loadGroups(batch.value.id), loadFailedCount(batch.value.id)])
    if (stopped) return
    batch.value = current
    await loadFallbackCounts(current)
    await reloadVisible()
    error.value = ''
  } catch (reason) {
    error.value = errorMessage(reason)
  } finally {
    loading.value = false
  }
}

async function boot() {
  loading.value = true
  try {
    const opened = props.batchId ? await getIntakeBatch(props.batchId) : await getCurrentIntakeBatch()
    if (stopped) return
    batch.value = opened
    emit('opened', opened.id)
    await Promise.all([loadGroups(opened.id), loadFailedCount(opened.id), loadFallbackCounts(opened)])
    await reloadVisible()
  } catch (reason) {
    if (reason instanceof ApiError && reason.status === 404 && props.batchId === null) batch.value = null
    else error.value = errorMessage(reason)
  } finally {
    loading.value = false
  }
}

/** Page splitting runs in the background, so a pending file is worth re-reading. */
async function process() {
  if (!batch.value || processing.value) return
  processing.value = true
  try {
    batch.value = (await processIntakeBatch(batch.value.id, true)).batch
    await refresh()
  } catch (reason) {
    error.value = errorMessage(reason)
  } finally {
    processing.value = false
  }
}

/** Recognition is an explicit action: scanned pages may be sent to the configured OCR provider. */
async function recognize(retryFailed = false) {
  if (!batch.value || recognizing.value) return
  const batchId = batch.value.id
  recognizing.value = true
  notice.value = '正在读取日期和商家；扫描页可能需要几分钟。'
  try {
    let count = 0
    let failedCount = 0
    for (;;) {
      const result = await recognizeIntakeBatch(batchId, retryFailed, retryFailed ? 50 : 1)
      if (stopped || batch.value?.id !== batchId) return
      if (!result.results.length) break
      count += result.results.length
      failedCount += result.results.filter((item) => item.outcome === 'failed').length
      await refresh()
      notice.value = `已读取 ${count} 项；可在右侧核对和修改建议分组。`
      // A failed item stays eligible for retry. Never retry it in a tight loop.
      if (retryFailed) break
    }
    notice.value = count
      ? `本次读取 ${count} 项${failedCount ? `，其中 ${failedCount} 项失败，可重试` : ''}。可在右侧核对和修改建议分组。`
      : '没有待识别的内容；已有建议会显示在右侧。'
  } catch (reason) {
    if (!stopped) error.value = errorMessage(reason)
  } finally {
    recognizing.value = false
  }
}

// --- 选择 -------------------------------------------------------------------

function toggle(item: IntakeItem) {
  selection.value = selectedIds.value.has(item.id)
    ? selection.value.filter((selected) => selected.id !== item.id)
    : [...selection.value, item]
}

function selectAllPending() {
  const items = pendingBucket.value?.items ?? []
  const missing = items.filter((item) => !selectedIds.value.has(item.id))
  selection.value = missing.length
    ? [...selection.value, ...missing]
    : selection.value.filter((item) => !items.some((loaded) => loaded.id === item.id))
}

function clearSelection() {
  selection.value = []
}

// --- 移动 -------------------------------------------------------------------

function nextRequestKey(action: string): string {
  moveCounter += 1
  return `org-${batch.value?.id ?? 0}-${action}-${Date.now().toString(36)}-${moveCounter}`
}

async function dispatchMove(zone: DropZone, items: IntakeItem[], reuseKey?: string, confirmed = false) {
  if (!batch.value || moving.value) return
  if (!canMove.value) {
    conflict.value = '这个批次已经收口或作废，不能再移动内容。'
    return
  }
  // Already in a draft: say why instead of letting the server refuse the batch.
  const locked = items.filter(isHandedOff)
  const movable = items.filter((item) => !isHandedOff(item))
  if (!movable.length) {
    conflict.value = locked.length === 1
      ? '这一项正在填表，整理页不能移动它；可在填表页修改分类，或用「撤回填表」退回。'
      : `选中的 ${locked.length} 项都在填表中，整理页不能移动它们；可在填表页修改分类，或先「撤回填表」。`
    return
  }
  const pending = movable.filter((item) => !alreadyThere(item, zone))
  if (!pending.length) {
    notice.value = `选中的内容已经在 ${zoneLabel(zone)}，这一步没有改动任何内容。`
    return
  }
  const action = zoneAction(zone)
  const target = zoneTarget(zone)
  // 把一张已经有商家的卡片放进另一家的组里，先问一句（第 17 节 ②）。
  if (action === 'group' && !confirmed) {
    const clashing = pending.filter((item) =>
      knownSourceOf(item) !== null && !sameSource(knownSourceOf(item), target.sourceName))
    if (clashing.length) {
      sourceClash.value = {
        zone, items: pending, target: target.sourceName ?? '',
        names: [...new Set(clashing.map((item) => knownSourceOf(item) as string))]
      }
      return
    }
  }
  const key = reuseKey ?? nextRequestKey(action)
  moving.value = true
  notice.value = ''
  try {
    const result = await moveIntakeItems(batch.value.id, {
      request_key: key,
      action,
      items: pending.map((item) => ({ id: item.id, version: item.version })),
      ...(action === 'date' || action === 'group' ? { purchase_date: target.purchaseDate as string } : {}),
      ...(action === 'source' || action === 'group' ? { source_name: target.sourceName as string } : {})
    })
    const moved = new Set(result.items.map((item) => item.id))
    selection.value = selection.value.filter((item) => !moved.has(item.id))
    const skipped = locked.length ? `，另有 ${locked.length} 项在填表中没有移动` : ''
    lastMove.value = {
      id: result.move_id,
      text: `已把 ${result.items.length} 项移到 ${zoneLabel(zone)}${skipped}`
    }
    failed.value = null
    conflict.value = ''
    error.value = ''
    if (zone.kind !== 'pending') expanded.value = [...new Set([...expanded.value, bucketKey(target)])]
    await refresh()
  } catch (reason) {
    if (reason instanceof ApiError && reason.status === 409) {
      // The server rolled the whole move back, so nothing moved by halves.
      conflict.value = `${reason.message}（没有内容被改动）`
      failed.value = null
      await refresh()
    } else {
      error.value = errorMessage(reason)
      failed.value = { key, zone, items: pending }
    }
  } finally {
    moving.value = false
  }
}

/**
 * 「合并过去」还是「只改日期」。
 *
 * 只改日期这一条走的是普通的 `date` 命令：卡片自己的商家（人定的或纸面读出的）
 * 一个字不动，于是它会在那一天下面**自成一组**，而不是并进别人家。
 */
function resolveSourceClash(keepSource: boolean) {
  const ask = sourceClash.value
  if (!ask) return
  sourceClash.value = null
  const target = zoneTarget(ask.zone)
  if (!keepSource) { void dispatchMove(ask.zone, ask.items, undefined, true); return }
  if (target.purchaseDate) void dispatchMove({ kind: 'date', purchaseDate: target.purchaseDate }, ask.items)
}

function retryFailedMove() {
  const pendingRetry = failed.value
  if (pendingRetry) void dispatchMove(pendingRetry.zone, pendingRetry.items, pendingRetry.key)
}

async function undoLast() {
  const move = lastMove.value
  if (!batch.value || !move || moving.value) return
  moving.value = true
  try {
    await undoIntakeMove(batch.value.id, move.id)
    lastMove.value = null
    conflict.value = ''
    notice.value = '已撤销上一步移动。'
    await refresh()
  } catch (reason) {
    if (reason instanceof ApiError && reason.status === 409) conflict.value = reason.message
    else error.value = errorMessage(reason)
  } finally {
    moving.value = false
  }
}

/** 移入所选 on a group, and the keyboard equivalent of dropping. */
function moveSelected(zone: DropZone) {
  if (selection.value.length) void dispatchMove(zone, [...selection.value])
}

// --- 送入审核（M08 的命令，闭环的下一步） -----------------------------------

/**
 * The selected contents currently displayed in this suggested or edited group.
 *
 * Handing off takes **what the user picked**, not the group: one group can hold
 * several real invoices, so the page must not decide where one invoice ends.
 */
function selectedIn(target: BoardTarget): IntakeItem[] {
  const ids = new Set((buckets.value[bucketKey(target)]?.items ?? []).map((item) => item.id))
  return selection.value.filter((item) => ids.has(item.id))
}

function handoffBlocked(target: BoardTarget): string {
  if (!selection.value.length) return '先勾选要一起填表的文件；只填一份的话，点那张卡片上的「开始填表」。'
  // 用户的原话（2026-11-24）：这正是按钮暗下来时鼠标悬停要看到的那句。
  if (selection.value.length !== selectedIn(target).length) return '无法同时入库来自不同日期或者不同来源的文件。'
  return handoffProblem(selectedIn(target), true)
}

/**
 * Why this one card cannot start a form by itself, or `''`.
 *
 * 单张不看别处的勾选：它要的只是这张卡片**当前所在的组**已经有日期和商家。
 */
function startProblem(item: IntakeItem, purchaseDate: string | null, sourceName: string | null): string {
  if (!canMove.value) return '这个批次已经结束，不能再开始填表。'
  if (purchaseDate === null && sourceName === null) return '先确认采购日期和商家，再开始填表。'
  if (purchaseDate === null) return '先确认采购日期，再开始填表。'
  if (sourceName === null) return '先确认商家，再开始填表。'
  return handoffProblem([item], true)
}

/** 卡片上的「开始填表」：只送这一张，不动用户已经勾上的其它内容。 */
function startOne(item: IntakeItem, purchaseDate: string | null, sourceName: string | null) {
  void handOff({ purchaseDate, sourceName }, [item], startProblem(item, purchaseDate, sourceName))
}

async function handOff(target: BoardTarget, picked?: IntakeItem[], blocked?: string) {
  if (!batch.value || handing.value || moving.value) return
  const items = picked ?? selectedIn(target)
  const problem = blocked ?? handoffBlocked(target)
  if (problem) { conflict.value = problem; return }
  handing.value = true
  notice.value = ''
  try {
    const result = await startIntakeForms(
      batch.value.id, items.map((item) => ({ id: item.id, version: item.version })),
      { purchase_date: target.purchaseDate!, source_name: target.sourceName! })
    conflict.value = ''
    error.value = ''
    lastMove.value = null
    const sent = new Set(items.map((item) => item.id))
    selection.value = selection.value.filter((item) => !sent.has(item.id))
    notice.value = result.created
      ? `已为 ${items.length} 项准备 ${result.invoice_ids.length} 张填表草稿。`
      : '这些内容已有填表草稿，没有重复建单。'
    await refresh()
    emit('review', result.invoice_ids)
  } catch (reason) {
    if (reason instanceof ApiError && reason.status === 409) {
      conflict.value = reason.message
      await refresh()
    } else error.value = errorMessage(reason)
  } finally {
    handing.value = false
  }
}

// --- 继续填表与撤回填表（第 11 节 ③④） --------------------------------------

/**
 * Back into the draft this card is being filled in.
 *
 * Without this the only way back was the 单据列表, which is exactly the detour
 * this page is supposed to remove: leaving the fill page used to leave the card
 * marked 编辑中 with no way in.
 */
function resumeForm(item: IntakeItem) {
  cardMenu.value = null
  if (item.review_invoice_id === null) {
    conflict.value = '这一项的填表草稿找不到了，请刷新整理页。'
    return
  }
  emit('review', [item.review_invoice_id])
}

/** Asks first: withdrawing throws away what was typed into that draft. */
function askWithdraw(items: IntakeItem[]) {
  cardMenu.value = null
  const editing = items.filter((item) => isHandedOff(item) && item.review_status !== 'completed')
  if (!editing.length) {
    conflict.value = items.some(isHandedOff)
      ? '选中的内容已经完成入库；要改它，请在单据里撤销确认或修改分类。'
      : '选中的内容都还没开始填表，不需要撤回。'
    return
  }
  withdrawResult.value = null
  withdrawAsk.value = editing
}

/** 关掉撤回对话框，连同它里面的结果。 */
function closeWithdraw() {
  withdrawAsk.value = null
  withdrawResult.value = null
}

async function confirmWithdraw() {
  const items = withdrawAsk.value
  if (!batch.value || !items || withdrawing.value) return
  withdrawing.value = true
  try {
    const result = await withdrawIntakeForms(
      batch.value.id, items.map((item) => ({ id: item.id, version: item.version })))
    selection.value = []
    lastMove.value = null
    conflict.value = ''
    error.value = ''
    // The server withdraws whole drafts, so this count can exceed the selection.
    const done = `已撤回 ${result.item_ids.length} 项的填表草稿，填过的 ${result.cleared_lines} 行已清空；日期和商家保留。`
    withdrawResult.value = { ok: true, text: done }
    notice.value = done
    await refresh()
  } catch (reason) {
    const failure = reason instanceof ApiError && reason.status === 409
      ? reason.message : errorMessage(reason)
    withdrawResult.value = { ok: false, text: failure }
    if (reason instanceof ApiError && reason.status === 409) {
      conflict.value = reason.message
      await refresh()
    } else error.value = errorMessage(reason)
  } finally {
    withdrawing.value = false
  }
}

/**
 * 重新填写：先问一句，因为它会清空这张单据，并把它已经入账的采购一并撤销。
 *
 * 只对**已完成**的卡片开口。还在填的那张有红色的「撤回填表」，两者落点不同
 * （一个回到「还没开始填」，一个直接落进新的空表），不合成一个按钮。
 */
function askRefill(item: IntakeItem) {
  cardMenu.value = null
  if (item.review_status !== 'completed') {
    conflict.value = '这一项还没有完成，要重来请用「撤回填表」。'
    return
  }
  refillError.value = ''
  refillAsk.value = item
}

function closeRefill() {
  refillAsk.value = null
  refillError.value = ''
}

async function confirmRefill() {
  const item = refillAsk.value
  if (!batch.value || !item || refilling.value) return
  refilling.value = true
  refillError.value = ''
  try {
    const result = await refillIntakeForm(batch.value.id, [{ id: item.id, version: item.version }])
    selection.value = []
    lastMove.value = null
    conflict.value = ''
    error.value = ''
    refillAsk.value = null
    // 落进那张新的空表。整页换掉本身就是反馈，所以这里不再弹一句话。
    emit('review', [result.invoice_id])
  } catch (reason) {
    refillError.value = reason instanceof ApiError && reason.status === 409
      ? reason.message : errorMessage(reason)
    if (reason instanceof ApiError && reason.status === 409) await refresh()
  } finally {
    refilling.value = false
  }
}

// --- 拖拽 -------------------------------------------------------------------

function beginDrag(item: IntakeItem, event: unknown) {
  if (!canMove.value || moving.value) return
  // Dragging a card that is not selected drags that card alone; dragging a
  // selected one takes the whole selection, with the count on the preview.
  dragItems = selectedIds.value.has(item.id) ? [...selection.value] : [item]
  const started = startBoardDrag({
    event,
    onStart: () => { dragging.value = { count: dragItems.length, x: 0, y: 0 } },
    onMove: (position, key) => {
      if (dragging.value) dragging.value = { count: dragItems.length, x: position.x, y: position.y }
      hoverKey.value = key
    },
    onEnd: (key) => {
      const zone = parseDropKey(key)
      const items = dragItems
      dragItems = []
      dragging.value = null
      hoverKey.value = null
      if (zone && items.length) void dispatchMove(zone, items)
    }
  })
  if (!started) dragItems = []
}

function zoneClass(zone: DropZone) {
  const key = dropKey(zone)
  return { droppable: dragging.value !== null, over: hoverKey.value === key }
}

// --- 更多菜单与移到面板 -----------------------------------------------------

function openCardMenu(item: IntakeItem) {
  cardMenu.value = item
}

function openPanel(items: IntakeItem[]) {
  if (!items.length) return
  cardMenu.value = null
  panel.value = {
    items, dateMode: 'pick', date: '', typedDate: '', sourceMode: 'unknown', source: '', typedSource: ''
  }
}

function panelDate(): string | null {
  const open = panel.value
  if (!open) return null
  const chosen = open.dateMode === 'type' ? open.typedDate.trim() : open.date
  return isIsoDate(chosen) ? chosen : null
}

/** `null` means 商家待定 — a deliberate choice, not a missing answer. */
function panelSource(): string | null {
  const open = panel.value
  if (!open || open.sourceMode === 'unknown') return null
  const name = open.sourceMode === 'new' ? open.typedSource.trim() : open.source
  return name.length ? name : null
}

const panelProblem = computed(() => {
  const open = panel.value
  if (!open) return ''
  if (open.dateMode === 'pick' && !open.date) return '先选一个日期，或改用「其它日期」填写。'
  if (!panelDate()) return '日期请按 2026-11-08 这样填写，并且确实存在。'
  if (open.sourceMode === 'new' && !open.typedSource.trim()) return '填写商家名，或改选「商家待定」。'
  if (open.sourceMode === 'pick' && !open.source) return '先选一个商家。'
  return ''
})

function pickPanelDate(value: string) {
  if (!panel.value) return
  panel.value.dateMode = 'pick'
  panel.value.date = value
}

function pickPanelSource(mode: 'unknown' | 'pick' | 'new', value = '') {
  if (!panel.value) return
  panel.value.sourceMode = mode
  panel.value.source = value
}

function submitPanel() {
  const open = panel.value
  if (!open || panelProblem.value) return
  const date = panelDate()
  if (!date) return
  const source = panelSource()
  const zone: DropZone = source === null
    ? { kind: 'date', purchaseDate: date }
    : { kind: 'group', purchaseDate: date, sourceName: source }
  const items = open.items
  panel.value = null
  void dispatchMove(zone, items)
}

async function openGroupEdit(kind: 'date' | 'source', targets: BoardTarget[], value: string, count: number) {
  if (!batch.value || !canMove.value || loadingGroupEdit.value) return
  if (count > 200) { error.value = '这一组超过 200 项，请在财务表格里分批修改。'; return }
  loadingGroupEdit.value = true
  try {
    const pages = await Promise.all(targets.map((target) => getIntakeGroupItems(batch.value!.id, target, 200)))
    if (pages.some((page) => page.total > 200)) { error.value = '这一组超过 200 项，请分批修改。'; return }
    const items = pages.flatMap((page) => page.items)
    if (!items.length) { error.value = '组内没有可修改的内容，请刷新。'; return }
    groupEdit.value = { kind, items, value, single: false, choices: [] }
    error.value = ''
  } catch (reason) { error.value = errorMessage(reason) }
  finally { loadingGroupEdit.value = false }
}

/**
 * 只改这一张卡片的商家（第 17 节 ①）。
 *
 * 用的是组上「修改商家」那条同一个命令（`action: "source"`，带版本检查与撤销
 * 记录），只是作用在一项上，日期一个字不动。面板上先给两类现成的名字：**这张纸
 * 自己读出的**（放错组之后它仍然在候选里，只是不再显示），和这一批里已有的商家。
 */
function openSourceEdit(item: IntakeItem) {
  cardMenu.value = null
  if (!canMove.value) { conflict.value = '这个批次已经结束，不能再修改商家。'; return }
  if (isHandedOff(item)) {
    conflict.value = '这一项正在填表，整理页不能改它的商家；可在填表页修改，或先「撤回填表」。'
    return
  }
  const paper = paperSourceOf(item)
  const choices = [...new Set([...(paper ? [paper] : []), ...knownSources.value])]
    .filter((name) => !sameSource(name, item.decided_source_name))
  groupEdit.value = {
    kind: 'source', items: [item], value: item.decided_source_name ?? paper ?? '',
    single: true, choices
  }
  error.value = ''
}

function saveGroupEdit() {
  const edit = groupEdit.value
  if (!edit) return
  const value = edit.value.trim()
  if (edit.kind === 'date' && !isIsoDate(value)) { error.value = '请填写合法的 YYYY-MM-DD 日期。'; return }
  if (edit.kind === 'source' && !value) { error.value = '请填写商家名称。'; return }
  groupEdit.value = null
  const zone: DropZone = edit.kind === 'date'
    ? { kind: 'date', purchaseDate: value } : { kind: 'source', sourceName: value }
  void dispatchMove(zone, edit.items)
}

function sendToPending(items: IntakeItem[]) {
  cardMenu.value = null
  if (items.length) void dispatchMove(PENDING_ZONE, items)
}

function confirmSuggestedDate(item: IntakeItem, date: string) {
  if (!isIsoDate(date)) { error.value = '日期建议无效，请在「移到…」里填写日期。'; return }
  const zone: DropZone = item.decided_source_name
    ? { kind: 'group', purchaseDate: date, sourceName: item.decided_source_name }
    : { kind: 'date', purchaseDate: date }
  void dispatchMove(zone, [item])
}

function openPreview(item: IntakeItem) {
  cardMenu.value = null
  preview.value = item
}

/**
 * The 更多 menu acts on the card it was opened from. These wrappers exist so
 * the template never has to narrow `cardMenu` itself.
 */
function menuPreview() {
  if (cardMenu.value) openPreview(cardMenu.value)
}

function menuPanel() {
  if (cardMenu.value) openPanel([cardMenu.value])
}

function menuPending() {
  if (cardMenu.value) sendToPending([cardMenu.value])
}

function menuToggle() {
  if (cardMenu.value) toggle(cardMenu.value)
  cardMenu.value = null
}

async function menuDelete() {
  const item = cardMenu.value
  if (!item) return
  cardMenu.value = null
  deleteAsk.value = item
  deleteInvoice.value = null
  deleteError.value = ''
  if (item.review_invoice_id === null) return
  deleteLoading.value = true
  try {
    deleteInvoice.value = await getInvoice(item.review_invoice_id)
  } catch (reason) {
    deleteError.value = errorMessage(reason)
  } finally {
    deleteLoading.value = false
  }
}

function closeDelete() {
  if (deleting.value) return
  deleteAsk.value = null
  deleteInvoice.value = null
  deleteError.value = ''
}

async function confirmDelete() {
  const item = deleteAsk.value
  if (!batch.value || !item || deleting.value || deleteLoading.value) return
  deleting.value = true
  deleteError.value = ''
  try {
    if (item.review_invoice_id !== null) {
      const invoice = deleteInvoice.value
      if (!invoice || invoice.id !== item.review_invoice_id) return
      const booked = invoice.lines.filter((line) => line.status === 'confirmed').map((line) => line.id)
      await voidInvoice(invoice.id, booked.length ? booked : undefined)
    } else {
      await deleteIntakeItem(batch.value.id, item.id, item.version)
    }
    selection.value = []
    lastMove.value = null
    deleteAsk.value = null
    deleteInvoice.value = null
    await refresh()
  } catch (reason) {
    deleteError.value = errorMessage(reason)
    if (reason instanceof ApiError && reason.status === 409) await refresh()
  } finally {
    deleting.value = false
  }
}

/** The viewer reads batch bytes through the content's own reference. */
const previewSource = computed<IntakeSource[]>(() => {
  const item = preview.value
  if (!item) return []
  return [{
    batch_id: item.batch_id, item_id: item.id, batch_file_id: item.batch_file_id,
    version: item.version, kind: item.kind, page_no: item.page_no, row_no: item.row_no,
    original_name: item.original_name, content_type: null
  }]
})

watch(() => props.batchId, (next, previous) => {
  if (next === previous || next === null) return
  batch.value = null
  groups.value = []
  buckets.value = {}
  expanded.value = []
  manuallyClosed.clear()
  selection.value = []
  void boot()
})
watch(() => props.active, (active) => {
  if (active && batch.value) void refresh()
})

onMounted(() => {
  void boot()
  stopViewport = watchViewport((value) => { narrow.value = value })
  // Only while splitting is still running: a finished batch is not re-read.
  poll = setInterval(() => {
    if (batch.value && !moving.value && !processing.value
      && batch.value.files.some((file) => file.status === 'pending')) void refresh()
  }, 3000)
})
onUnmounted(() => {
  stopped = true
  stopViewport()
  cancelBoardDrag()
  if (poll) clearInterval(poll)
})
</script>

<template>
  <view class="organizer">
    <view class="ui-card head">
      <!-- 标题右边的「▾」是这一批的**显示形式**：卡片整理 / 财务表格。
           它原来是工具栏上一个叫「财务表格」的按钮，和「刷新」「开始识别」这些
           **动作**挤在一排，而它根本不是动作，是换一种看法（第 18 节 ①）。 -->
      <!-- ⚠️ 标题上**不写批次号**（第 19 节）。它是 `intake_batches.id`，库里的主键，
           而界面上从来没有「在几个批次里挑一个」这回事：要么是刚传完的这一批，
           要么是首页「接着整理」打开的 `/current`。它真正的用处在 URL
           （`?view=organize&batch=2`）、接口和日志里——同第 13 节 ① 删掉「单据 #27」
           的那条理由。`data-batch-id` 留在元素上，浏览器实跑仍认得出是哪一批。 -->
      <view class="title-row" :data-batch-id="batch ? batch.id : undefined">
        <text class="title">按日期和商家整理</text>
        <view v-if="!narrow" class="view-switch">
          <button class="caret" :aria-expanded="viewMenu ? 'true' : 'false'" aria-label="切换显示形式"
            @click="viewMenu = !viewMenu">▾</button>
          <view v-if="viewMenu" class="view-menu">
            <button class="ui-button ghost" @click="viewMenu = false; emit('ledger')">财务表格形式</button>
          </view>
        </view>
      </view>
      <text class="muted">点「开始/继续识别」读取日期和商家建议。扫描页可能发送到已配置的 OCR 服务；系统建议和人工修改会分别标明。</text>
      <view class="actions">
        <button class="ui-button secondary" @click="emit('upload')">返回上传</button>
        <button class="ui-button secondary" :disabled="loading || moving" @click="refresh(true)">刷新</button>
        <button class="ui-button" :disabled="!batch || recognizing || processing" @click="recognize(false)">开始/继续识别</button>
        <!-- 没有失败项时这个按钮什么也做不了，所以干脆不出现（第 18 节 ①）。 -->
        <button v-if="failedCount > 0" class="ui-button secondary" :disabled="!batch || recognizing || processing"
          @click="recognize(true)">重试识别失败项（{{ failedCount }}）</button>
        <button v-if="lastMove" class="ui-button secondary" :disabled="moving" @click="undoLast">撤销上一步</button>
      </view>
      <text v-if="batch" class="muted counts">
        待归类 {{ board.pendingCount }} 项 · 右侧已整理 {{ groupedTotal }} 项 · 共 {{ contentTotal }} 项
      </text>
      <text v-if="batch && !canMove" class="warn-text">这个批次已经结束，只能查看，不能再移动内容。</text>
      <text v-if="recognizing" class="muted" role="status">正在识别，请等待结果；刷新不会启动识别。</text>
    </view>

    <view v-if="unsettledFiles.length" class="ui-card notice">
      <text class="notice-title">有文件还没处理完</text>
      <text v-for="file in unsettledFiles" :key="file.id" class="muted">
        {{ file.original_name }} · {{ file.status === 'pending' ? '等待切页' : '处理失败' }}{{ file.error ? ` · ${file.error.message}` : '' }}
      </text>
      <button class="ui-button secondary" :disabled="processing" @click="process">重试文件处理</button>
    </view>

    <view v-if="lastMove" class="ui-card notice undo" role="status">
      <text>{{ lastMove.text }}</text>
      <button class="ui-button secondary" :disabled="moving" @click="undoLast">撤销这一步</button>
    </view>
    <view v-if="conflict" class="ui-card notice conflict" role="alert">
      <text class="conflict-text">{{ conflict }}</text>
      <text class="muted">页面已经重新读取服务端的最新结果，请确认后再移动。</text>
    </view>
    <view v-if="error" class="ui-card notice conflict" role="alert">
      <text class="conflict-text">{{ error }}</text>
      <button v-if="failed" class="ui-button secondary" :disabled="moving" @click="retryFailedMove">重试这一步</button>
    </view>
    <text v-if="notice" class="muted notice-line" role="status">{{ notice }}</text>

    <text v-if="loading" class="muted" role="status">正在读取整理批次…</text>
    <view v-else-if="!batch" class="ui-card empty">
      <text>当前没有正在整理的批次。</text>
      <button class="ui-button" @click="emit('upload')">去上传文件</button>
    </view>
    <template v-else>
      <view v-if="narrow" class="pane-switch" role="tablist">
        <button class="pane-tab" :class="{ active: pane === 'pending' }" role="tab"
          :aria-selected="pane === 'pending' ? 'true' : 'false'" @click="pane = 'pending'">
          待归类 {{ board.pendingCount }}
        </button>
        <button class="pane-tab" :class="{ active: pane === 'board' }" role="tab"
          :aria-selected="pane === 'board' ? 'true' : 'false'" @click="pane = 'board'">
          日期分组 {{ board.dated.length }}
        </button>
      </view>

      <view class="layout" :class="{ narrow }">
        <view v-if="!narrow || pane === 'pending'" class="ui-card column pending" :class="zoneClass(PENDING_ZONE)"
          :data-drop-key="dropKey(PENDING_ZONE)">
          <view class="column-head">
            <text class="section-title">待归类 · {{ board.pendingCount }} 项</text>
            <button v-if="pendingBucket?.items.length" class="ui-button ghost small" @click="selectAllPending">
              {{ pendingBucket.items.every(item => selectedIds.has(item.id)) ? '取消本页选择' : '选中本页' }}
            </button>
          </view>
          <text class="muted hint">
            {{ narrow ? '先选中内容，再切到「日期分组」点目标组。' : canDrag ? '按住卡片右边的手柄拖动：拖到日期标题只定日期，拖到商家组同时定日期和商家。也可以选中后点目标组的按钮。' : '选中内容后点目标组的按钮完成归类。' }}
          </text>
          <view v-if="pendingBucket?.error" class="warn-text" role="alert">{{ pendingBucket.error }}</view>
          <text v-if="pendingBucket?.loading && !pendingBucket.items.length" class="muted" role="status">正在读取…</text>
          <view v-else-if="!pendingBucket?.items.length" class="empty-inline">
            <text v-if="contentTotal === 0">这一批还没有可整理的内容。文件切页完成后，页和表格行会出现在这里。</text>
            <text v-else>待归类是空的——这一批的内容都已经有日期或商家了。</text>
          </view>
          <view v-else class="cards">
            <IntakeCard v-for="item in pendingBucket.items" :key="item.id" :item="item"
              :selected="selectedIds.has(item.id)" :draggable="canDrag && canMove" :confirmable="canMove && !moving"
              :proposed-date="false" :proposed-source="false" :start-problem="startProblem(item, null, null)"
              @toggle="toggle(item)" @preview="openPreview(item)" @menu="openCardMenu(item)"
              @drag="beginDrag(item, $event)" @confirm-date="confirmSuggestedDate(item, $event)"
              @resume="resumeForm(item)" @withdraw="askWithdraw([item])" @refill="askRefill(item)" @start="startOne(item, null, null)"
              @edit-source="openSourceEdit(item)" />
            <button v-if="pendingBucket.items.length < pendingBucket.total" class="ui-button secondary"
              :disabled="pendingBucket.loading" @click="loadBucket(PENDING, true)">
              加载更多（还有 {{ pendingBucket.total - pendingBucket.items.length }} 项）
            </button>
          </view>
        </view>

        <view v-if="!narrow || pane === 'board'" class="column groups">
          <view class="ui-card new-date">
            <text class="section-title">新建日期类别</text>
            <DateFieldInput v-model="newDate" label="新日期类别" :disabled="dateBusy || !canMove" />
            <button class="ui-button" :disabled="dateBusy || !canMove || !isIsoDate(newDate.trim())" @click="addDateGroup">新建日期</button>
          </view>
          <view v-if="!board.dated.length && !board.undated.length" class="ui-card empty">
            <text>还没有任何日期分组。</text>
            <text class="muted">识别出可用的日期或商家后会自动出现在右侧；也可手动用「移到…」归组。</text>
            <button class="ui-button" :disabled="!selection.length" @click="openPanel([...selection])">移到…</button>
          </view>
          <!-- ⚠️ 整块都是「只定这一天」的投放区（第 17 节 ②），不只是标题那一条：
               用户说的「拖到 Day1 里面」指的就是这一整块。里面的商家组各有自己的
               `data-drop-key`，`closest()` 取最近的那个，所以放到某一家头上仍然是
               「日期和商家一起定」。 -->
          <view v-for="section in board.dated" :key="section.purchaseDate" class="ui-card section"
            :class="zoneClass({ kind: 'date', purchaseDate: section.purchaseDate })"
            :data-drop-key="dropKey({ kind: 'date', purchaseDate: section.purchaseDate })">
            <view class="section-head" :class="zoneClass({ kind: 'date', purchaseDate: section.purchaseDate })">
              <view class="section-title-wrap">
                <text class="date-label">{{ monthDayLabel(section.purchaseDate) }}</text>
                <text class="muted">{{ yearLabel(section.purchaseDate) }} · {{ section.itemCount }} 项<text v-if="section.suggestedCount"> · 系统建议 {{ section.suggestedCount }} 项</text></text>
              </view>
              <button class="ui-button ghost small" :disabled="loadingGroupEdit || moving || !canMove"
                @click="openGroupEdit('date', section.groups, section.purchaseDate, section.itemCount)">修改日期</button>
              <button v-if="section.itemCount === 0" class="ui-button ghost small" :disabled="dateBusy || !canMove"
                @click="dateToDelete = section.purchaseDate">删除类别</button>
              <button class="ui-button secondary small" :disabled="!selection.length || moving || !canMove"
                @click="moveSelected({ kind: 'date', purchaseDate: section.purchaseDate })">
                只定这一天（{{ selection.length }}）
              </button>
            </view>
            <text v-if="section.itemCount === 0" class="muted empty-inline">这个日期暂无内容，可把原件拖到这里，或选中后点「只定这一天」。</text>
            <view v-for="group in section.groups" :key="group.sourceName ?? 'unknown'" class="group"
              :class="zoneClass({ kind: 'group', purchaseDate: section.purchaseDate, sourceName: group.sourceName })"
              :data-drop-key="dropKey({ kind: 'group', purchaseDate: section.purchaseDate, sourceName: group.sourceName })">
              <view class="group-head">
                <text class="group-name" :class="{ unknown: group.sourceName === null }">
                  {{ group.sourceName ?? '商家待定' }} · {{ group.itemCount }} 项
                </text>
                <text v-if="group.suggestedCount" class="muted">系统建议 {{ group.suggestedCount }} 项</text>
                <view class="group-actions">
                  <button class="ui-button ghost small" :disabled="loadingGroupEdit || moving || !canMove"
                    @click="openGroupEdit('source', [{ purchaseDate: section.purchaseDate, sourceName: group.sourceName }], group.sourceName ?? '', group.itemCount)">修改商家</button>
                  <button class="ui-button secondary small" :disabled="!selection.length || moving || !canMove"
                    @click="moveSelected({ kind: 'group', purchaseDate: section.purchaseDate, sourceName: group.sourceName })">
                    移入所选（{{ selection.length }}）
                  </button>
                  <button v-if="group.sourceName !== null" class="ui-button small"
                    :disabled="handing || moving || !canMove
                      || handoffBlocked({ purchaseDate: section.purchaseDate, sourceName: group.sourceName }) !== ''"
                    :title="handoffBlocked({ purchaseDate: section.purchaseDate, sourceName: group.sourceName })"
                    @click="handOff({ purchaseDate: section.purchaseDate, sourceName: group.sourceName })">
                    勾选多个文件填表
                  </button>
                  <button class="ui-button ghost small"
                    @click="toggleExpanded({ purchaseDate: section.purchaseDate, sourceName: group.sourceName })">
                    {{ isExpanded({ purchaseDate: section.purchaseDate, sourceName: group.sourceName }) ? '收起' : '查看内容' }}
                  </button>
                </view>
              </view>
              <view v-if="isExpanded({ purchaseDate: section.purchaseDate, sourceName: group.sourceName })" class="cards">
                <text v-if="buckets[bucketKey({ purchaseDate: section.purchaseDate, sourceName: group.sourceName })]?.loading
                  && !buckets[bucketKey({ purchaseDate: section.purchaseDate, sourceName: group.sourceName })]?.items.length"
                  class="muted" role="status">正在读取…</text>
                <text v-else-if="buckets[bucketKey({ purchaseDate: section.purchaseDate, sourceName: group.sourceName })]?.error"
                  class="conflict-text" role="alert">
                  {{ buckets[bucketKey({ purchaseDate: section.purchaseDate, sourceName: group.sourceName })].error }}
                </text>
                <IntakeCard
                  v-for="item in buckets[bucketKey({ purchaseDate: section.purchaseDate, sourceName: group.sourceName })]?.items ?? []"
                  :key="item.id" :item="item" :selected="selectedIds.has(item.id)" :draggable="canDrag && canMove" :confirmable="canMove && !moving"
                  :proposed-date="!item.decided_purchase_date" :proposed-source="!item.decided_source_name && group.sourceName !== null"
                  :start-problem="startProblem(item, section.purchaseDate, group.sourceName)"
                  @toggle="toggle(item)" @preview="openPreview(item)" @menu="openCardMenu(item)"
                  @drag="beginDrag(item, $event)" @confirm-date="confirmSuggestedDate(item, $event)"
                  @resume="resumeForm(item)" @withdraw="askWithdraw([item])" @refill="askRefill(item)"
                  @start="startOne(item, section.purchaseDate, group.sourceName)"
                  @edit-source="openSourceEdit(item)" />
                <button
                  v-if="(buckets[bucketKey({ purchaseDate: section.purchaseDate, sourceName: group.sourceName })]?.items.length ?? 0)
                    < (buckets[bucketKey({ purchaseDate: section.purchaseDate, sourceName: group.sourceName })]?.total ?? 0)"
                  class="ui-button secondary"
                  @click="loadBucket({ purchaseDate: section.purchaseDate, sourceName: group.sourceName }, true)">
                  加载更多
                </button>
              </view>
            </view>
          </view>

          <view v-if="board.undated.length" class="ui-card section">
            <view class="section-head plain">
              <view class="section-title-wrap">
                <text class="date-label">只定了商家，还缺日期</text>
                <text class="muted">给它们补一个采购日期，就会进入那一天的分组。</text>
              </view>
            </view>
            <view v-for="group in board.undated" :key="group.sourceName ?? 'unknown'" class="group">
              <view class="group-head">
                <text class="group-name">{{ group.sourceName }} · {{ group.itemCount }} 项</text>
                <button class="ui-button ghost small" :disabled="loadingGroupEdit || moving || !canMove"
                  @click="openGroupEdit('source', [group], group.sourceName ?? '', group.itemCount)">修改商家</button>
                <button class="ui-button ghost small" @click="toggleExpanded(group)">
                  {{ isExpanded(group) ? '收起' : '查看内容' }}
                </button>
              </view>
              <view v-if="isExpanded(group)" class="cards">
                <IntakeCard v-for="item in buckets[bucketKey(group)]?.items ?? []" :key="item.id" :item="item"
                  :selected="selectedIds.has(item.id)" :draggable="canDrag && canMove" :confirmable="canMove && !moving"
                  :proposed-date="false" :proposed-source="!item.decided_source_name"
                  :start-problem="startProblem(item, null, group.sourceName)"
                  @toggle="toggle(item)" @preview="openPreview(item)" @menu="openCardMenu(item)"
                  @drag="beginDrag(item, $event)" @confirm-date="confirmSuggestedDate(item, $event)"
                  @resume="resumeForm(item)" @withdraw="askWithdraw([item])" @refill="askRefill(item)"
                  @start="startOne(item, null, group.sourceName)"
                  @edit-source="openSourceEdit(item)" />
              </view>
            </view>
          </view>
          <!-- 这一批填完了就在这里收口（第 22 节）。它在**最后一组的下面**，
               因为这是整理这件事做完的地方；还剩项没填完时它自己暗着并说出还差什么
               （第 13 节 ⑥），而不是点下去才被拒。 -->
          <view v-if="canMove" class="ui-card finish">
            <view class="finish-row">
              <text v-if="finishBlocked" class="muted finish-note">{{ finishBlocked }}</text>
              <text v-else class="muted finish-note">这一批的 {{ itemCount }} 项全部填完了。收口之后这一批不再接收新文件，首页的「接着整理」会消失。</text>
              <button class="ui-button finish-all" :disabled="!!finishBlocked || finishing"
                :title="finishBlocked || undefined" @click="finishBatch">确认完成所有填写</button>
            </view>
            <text v-if="finishError" class="conflict-text" role="alert">{{ finishError }}</text>
          </view>
        </view>
      </view>
    </template>

    <view v-if="selection.length" class="select-bar">
      <text class="select-count">已选 {{ selection.length }} 项</text>
      <view class="select-actions">
        <button class="ui-button" :disabled="moving || !canMove" @click="openPanel([...selection])">移到…</button>
        <button class="ui-button secondary" :disabled="moving || !canMove" @click="sendToPending([...selection])">移回待归类</button>
        <button v-if="selection.some((item) => isHandedOff(item) && item.review_status !== 'completed')"
          class="ui-button secondary" :disabled="withdrawing" @click="askWithdraw([...selection])">
          撤回填表（{{ selection.filter((item) => isHandedOff(item) && item.review_status !== 'completed').length }}）
        </button>
        <button class="ui-button secondary" @click="clearSelection">清空选择</button>
      </view>
    </view>

    <view v-if="groupEdit" class="overlay" @click.self="groupEdit = null">
      <view class="ui-card sheet">
        <text class="sheet-title">
          {{ groupEdit.single
            ? `修改「${groupEdit.items[0].original_name} ${cardLocation(groupEdit.items[0])}」的商家`
            : `修改这 ${groupEdit.items.length} 项的${groupEdit.kind === 'date' ? '日期' : '商家'}` }}
        </text>
        <text class="muted">
          {{ groupEdit.single
            ? '只改这一张；它的采购日期不动。改完它会落到那一天的这家商家下面。'
            : '系统建议尚未写成人工决定；这里的修改会应用到这一组的所有内容。' }}
        </text>
        <view v-if="groupEdit.choices.length" class="choices">
          <button v-for="name in groupEdit.choices" :key="name" class="choice"
            :class="{ active: groupEdit.value === name }" @click="groupEdit.value = name">{{ name }}</button>
        </view>
        <DateFieldInput v-if="groupEdit.kind === 'date'" v-model="groupEdit.value" label="新日期" />
        <input v-else v-model="groupEdit.value" class="ui-input" placeholder="商家名称" aria-label="新商家名称" />
        <view class="sheet-actions">
          <button class="ui-button" @click="saveGroupEdit">保存</button>
          <button class="ui-button secondary" @click="groupEdit = null">取消</button>
        </view>
      </view>
    </view>

    <view v-if="sourceClash" class="overlay" @click.self="sourceClash = null">
      <view class="ui-card sheet" role="dialog" aria-modal="true" aria-label="这些内容已经有商家">
        <text class="sheet-title">这 {{ sourceClash.items.length }} 项已经有商家</text>
        <text class="muted">
          放进「{{ sourceClash.target }}」会把它们的商家改成「{{ sourceClash.target }}」。
          现在读到的是：{{ sourceClash.names.join('、') }}。
        </text>
        <text class="muted">只改日期的话，它们会在那一天下面自成一组，商家保持不变。</text>
        <view class="sheet-actions">
          <button class="ui-button" :disabled="moving" @click="resolveSourceClash(true)">只改日期，保留原商家</button>
          <button class="ui-button secondary" :disabled="moving" @click="resolveSourceClash(false)">
            改成「{{ sourceClash.target }}」
          </button>
          <button class="ui-button ghost" @click="sourceClash = null">取消</button>
        </view>
      </view>
    </view>

    <view v-if="withdrawAsk" class="overlay" @click.self="closeWithdraw">
      <view v-if="withdrawResult" class="ui-card sheet" role="dialog" aria-modal="true" aria-label="撤回填表的结果">
        <text class="sheet-title">{{ withdrawResult.ok ? '已撤回填表' : '撤回没有成功' }}</text>
        <text :class="withdrawResult.ok ? 'muted' : 'conflict-text'" role="status">{{ withdrawResult.text }}</text>
        <view class="sheet-actions"><button class="ui-button" @click="closeWithdraw">知道了</button></view>
      </view>
      <view v-else class="ui-card sheet" role="dialog" aria-modal="true" aria-label="撤回填表">
        <text class="sheet-title">撤回这 {{ withdrawAsk.length }} 项的填表？</text>
        <text class="muted">
          回到待填表状态会清空这{{ withdrawAsk.length > 1 ? '些' : '张' }}单据已经填写的数据，是否继续？
        </text>
        <text class="muted">
          日期和商家会保留，内容回到分类好、还没开始填的状态；要连分类一起清掉，撤回后再用「移回待归类」。
          同一张草稿里的其它页会一起撤回。
        </text>
        <view class="sheet-actions">
          <button class="ui-button" :disabled="withdrawing" @click="confirmWithdraw">继续，清空填写数据</button>
          <button class="ui-button secondary" :disabled="withdrawing" @click="closeWithdraw">取消</button>
        </view>
      </view>
    </view>

    <view v-if="refillAsk" class="overlay" @click.self="closeRefill">
      <view class="ui-card sheet" role="dialog" aria-modal="true" aria-label="重新填写">
        <text class="sheet-title">重新填写这张单据？</text>
        <text class="muted">确认清空数据，重新填写吗？</text>
        <text class="muted">
          这张单据已经完成。清空之后它填过的行没有了，已经入账的采购记录会一并撤销，价格历史里不再计入；
          日期和商家保留，同一张单据里的其它页一起重来。
        </text>
        <text v-if="refillError" class="conflict-text" role="alert">{{ refillError }}</text>
        <view class="sheet-actions">
          <button class="ui-button" :disabled="refilling" @click="confirmRefill">
            {{ refilling ? '正在清空…' : '继续，清空并重新填写' }}
          </button>
          <button class="ui-button secondary" :disabled="refilling" @click="closeRefill">取消</button>
        </view>
      </view>
    </view>

    <view v-if="dateToDelete" class="overlay" @click.self="dateToDelete = null">
      <view class="ui-card sheet" role="dialog" aria-modal="true" aria-label="删除日期类别">
        <text class="sheet-title">删除 {{ monthDayLabel(dateToDelete) }} 类别？</text>
        <text class="muted">这个日期类别为空，删除后将不再显示。</text>
        <view class="sheet-actions">
          <button class="ui-button" :disabled="dateBusy" @click="removeDateGroup">删除类别</button>
          <button class="ui-button secondary" @click="dateToDelete = null">取消</button>
        </view>
      </view>
    </view>

    <view v-if="cardMenu" class="overlay" @click.self="cardMenu = null">
      <view class="ui-card sheet">
        <text class="sheet-title">{{ cardMenu.original_name }}</text>
        <text class="muted">{{ cardLocation(cardMenu) }}</text>
        <button class="ui-button secondary" @click="menuPreview">看原件</button>
        <template v-if="isHandedOff(cardMenu)">
          <!-- 有草稿的卡片只有两条路：回到那张草稿，或者撤回填表。撤回填表 2026-11-24
               起是卡片上的红按钮（第 13 节 ②），这里不再重复一遍；已完成的「重新填写」
               同理只在卡片上（第 20 节），菜单里只留看那张单据。 -->
          <button class="ui-button" @click="resumeForm(cardMenu)">
            {{ cardMenu.review_status === 'completed' ? '查看单据' : '继续填表' }}
          </button>
        </template>
        <template v-else>
          <button class="ui-button" :disabled="!canMove" @click="menuPanel">把这一张移到…</button>
          <button class="ui-button secondary" :disabled="!canMove" @click="menuPending">移回待归类</button>
        </template>
        <button class="ui-button secondary" @click="menuToggle">
          {{ selectedIds.has(cardMenu.id) ? '取消选中' : '选中这一张' }}
        </button>
        <button class="ui-button danger" :disabled="!canMove && cardMenu.review_invoice_id === null"
          @click="menuDelete">删除</button>
        <button class="ui-button ghost" @click="cardMenu = null">关闭</button>
      </view>
    </view>

    <view v-if="deleteAsk" class="overlay" @click.self="closeDelete">
      <view class="ui-card sheet" role="dialog" aria-modal="true" aria-label="删除本张单据">
        <text class="sheet-title">删除{{ deleteAsk.review_invoice_id !== null ? '本张单据' : '这一张内容' }}？</text>
        <text class="muted">{{ deleteAsk.original_name }} · {{ cardLocation(deleteAsk) }}</text>
        <text v-if="deleteLoading" class="muted">正在核对单据和入账记录…</text>
        <template v-else-if="deleteAsk.review_invoice_id !== null && deleteInvoice">
          <text class="muted">这张单据填过的内容会清空，关联的 {{ deleteInvoice.intake_sources.length }} 项内容会从整理页一起移除。</text>
          <text v-if="deleteInvoice.lines.some((line) => line.status === 'confirmed')" class="conflict-text">
            已入账的 {{ deleteInvoice.lines.filter((line) => line.status === 'confirmed').length }} 行采购记录会作废，不再计入价格统计；修改历史保留。
          </text>
        </template>
        <text v-else-if="deleteAsk.review_invoice_id === null" class="muted">这张内容及其分类、识别建议会从整理页移除。若它是文件的最后一项，重新上传该文件会从头开始。</text>
        <text v-if="deleteError" class="conflict-text" role="alert">{{ deleteError }}</text>
        <view class="sheet-actions">
          <button class="ui-button danger" :disabled="deleting || deleteLoading || (deleteAsk.review_invoice_id !== null && !deleteInvoice)"
            @click="confirmDelete">{{ deleting ? '正在删除…' : '确认删除' }}</button>
          <button class="ui-button secondary" :disabled="deleting" @click="closeDelete">取消</button>
        </view>
      </view>
    </view>

    <view v-if="panel" class="overlay" @click.self="panel = null">
      <view class="ui-card sheet wide">
        <text class="sheet-title">把 {{ panel.items.length }} 项移到</text>
        <text class="field-label">采购日期</text>
        <view class="choices">
          <button v-for="section in board.dated" :key="section.purchaseDate" class="choice"
            :class="{ active: panel.dateMode === 'pick' && panel.date === section.purchaseDate }"
            @click="pickPanelDate(section.purchaseDate)">
            {{ monthDayLabel(section.purchaseDate) }}
          </button>
          <button class="choice" :class="{ active: panel.dateMode === 'type' }" @click="panel.dateMode = 'type'">
            其它日期
          </button>
        </view>
        <DateFieldInput v-if="panel.dateMode === 'type'" v-model="panel.typedDate" label="采购日期" />
        <text class="field-label">商家</text>
        <view class="choices">
          <button class="choice" :class="{ active: panel.sourceMode === 'unknown' }" @click="pickPanelSource('unknown')">
            商家待定（只定日期）
          </button>
          <button v-for="name in knownSources" :key="name" class="choice"
            :class="{ active: panel.sourceMode === 'pick' && panel.source === name }"
            @click="pickPanelSource('pick', name)">{{ name }}</button>
          <button class="choice" :class="{ active: panel.sourceMode === 'new' }" @click="pickPanelSource('new')">
            新的商家
          </button>
        </view>
        <input v-if="panel.sourceMode === 'new'" v-model="panel.typedSource" class="ui-input" placeholder="纸面上的商家名"
          aria-label="商家名" />
        <text v-if="panelProblem" class="warn-text">{{ panelProblem }}</text>
        <view class="sheet-actions">
          <button class="ui-button" :disabled="!!panelProblem || moving" @click="submitPanel">确定移动</button>
          <button class="ui-button secondary" @click="panel = null">取消</button>
        </view>
      </view>
    </view>

    <view v-if="preview" class="overlay" @click.self="preview = null">
      <view class="ui-card preview-card">
        <view class="preview-head">
          <view class="preview-text">
            <text class="sheet-title">{{ preview.original_name }} · {{ cardLocation(preview) }}</text>
            <text v-if="preview.decided_purchase_date || preview.decided_source_name" class="muted">
              已确认：{{ preview.decided_purchase_date ? monthDayLabel(preview.decided_purchase_date) : '日期待定' }}
              / {{ preview.decided_source_name ?? '商家待定' }}
            </text>
            <text v-for="chip in suggestionsOf(preview)" :key="chip.id" class="muted">
              建议 {{ chip.text }} · {{ chip.basis }}（未确认）
            </text>
            <text v-if="recognitionNote(preview)" class="muted">{{ recognitionNote(preview) }}</text>
          </view>
          <button class="ui-button secondary" @click="preview = null">关闭</button>
        </view>
        <OriginalDocumentViewer class="preview-viewer" :files="[]" :sources="previewSource" eyebrow="批次原件"
          :is-fake="preview.recognition.is_fake === true" :focus="null" :visible="true" />
      </view>
    </view>

    <view v-if="dragging" class="drag-ghost" :style="{ left: `${dragging.x}px`, top: `${dragging.y}px` }">
      <text>{{ dragging.count }} 项</text>
    </view>
  </view>
</template>

<style scoped>
.organizer { display: flex; flex-direction: column; gap: 12px; padding-bottom: 96px; }
.head { display: flex; flex-direction: column; gap: 10px; padding: 20px; }
.title-row { position: relative; display: flex; align-items: center; gap: 6px; }
.title { font-size: 21px; font-weight: 700; }
/* 「▾」挨着标题：它说的是「这一批还能怎么看」，不是一个动作。 */
.view-switch { position: relative; display: flex; align-items: center; }
.caret { width: 36px; min-height: 36px; margin: 0; padding: 0; border: 0; background: transparent; color: var(--green); font-size: 16px; line-height: 1; cursor: pointer; }
.caret::after { border: 0; }
.view-menu { position: absolute; top: calc(100% + 4px); left: 0; z-index: 8; min-width: 160px; padding: 6px; border: 1px solid var(--line); border-radius: 8px; background: #fff; box-shadow: 0 10px 28px #173b2e20; }
.view-menu .ui-button { width: 100%; margin: 0; text-align: left; }
.muted { color: var(--muted); font-size: 13px; line-height: 1.6; }
.counts { font-size: 13px; font-weight: 650; }
.actions { display: flex; flex-wrap: wrap; gap: 10px; }
.warn-text { color: var(--warning); font-size: 13px; }
.notice { display: flex; flex-direction: column; gap: 8px; padding: 14px 18px; }
.notice-title { font-weight: 650; }
.notice.undo { flex-direction: row; align-items: center; justify-content: space-between; gap: 12px; border-color: var(--green); background: #f3f8f3; }
.notice.conflict { border-color: var(--error); }
.conflict-text { color: var(--error); font-size: 14px; line-height: 1.6; }
.ui-button.danger { border-color: var(--error); background: var(--error); color: #fff; }
.ui-button.danger:active { background: #812f25; }
.notice-line { padding: 0 4px; }
.empty { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; padding: 20px; }
.empty-inline { padding: 8px 0; color: var(--muted); font-size: 13px; line-height: 1.6; }
.pane-switch { display: flex; gap: 8px; }
.pane-tab { flex: 1; min-height: 44px; margin: 0; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: #fff; color: var(--muted); font-size: 14px; font-weight: 650; cursor: pointer; }
.pane-tab::after { border: 0; }
.pane-tab.active { border-color: var(--green); background: #eef4ec; color: var(--ink); }
.layout { display: grid; grid-template-columns: minmax(300px, 360px) 1fr; gap: 12px; align-items: start; }
.layout.narrow { grid-template-columns: 1fr; }
.column { min-width: 0; }
.pending { display: flex; flex-direction: column; gap: 10px; padding: 16px; }
.column-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
.section-title { font-size: 16px; font-weight: 650; }
.hint { font-size: 12px; }
.cards { display: flex; flex-direction: column; gap: 8px; }
.groups { display: flex; flex-direction: column; gap: 12px; }
.section { display: flex; flex-direction: column; gap: 8px; padding: 14px 16px; }
.section-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; padding: 8px; border: 1px dashed transparent; border-radius: 8px; }
.section-title-wrap { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.date-label { font-size: 18px; font-weight: 700; }
.group { display: flex; flex-direction: column; gap: 8px; padding: 10px; border: 1px solid var(--line); border-radius: 8px; }
.group-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
.group-name { font-size: 15px; font-weight: 650; overflow-wrap: anywhere; }
.group-name.unknown { color: var(--warning); }
.group-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.ui-button.small { min-height: 44px; padding: 6px 10px; font-size: 13px; font-weight: 500; }
/* A drop zone only announces itself while something is being dragged. */
.droppable { border: 1px dashed var(--green); background: #f6faf6; }
.over { border: 2px solid var(--green); background: #e6f1e9; }
.finish { display: flex; flex-direction: column; gap: 8px; }
.finish-row { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 12px; }
.finish-note { flex: 1 1 220px; text-align: right; }
.select-bar { position: fixed; left: 0; right: 0; bottom: 0; z-index: 12; display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; padding: 12px 20px calc(12px + env(safe-area-inset-bottom)); border-top: 1px solid var(--line); background: #fff; }
.select-count { font-size: 15px; font-weight: 650; }
.select-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.overlay { position: fixed; inset: 0; z-index: 20; display: flex; align-items: center; justify-content: center; padding: 16px; background: rgba(23, 59, 46, .45); }
.sheet { width: min(100%, 420px); max-height: 86vh; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; padding: 20px; }
.sheet.wide { width: min(100%, 520px); }
.sheet-title { font-size: 16px; font-weight: 700; overflow-wrap: anywhere; }
.sheet-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 4px; }
.field-label { margin-top: 6px; font-size: 13px; font-weight: 650; }
.choices { display: flex; flex-wrap: wrap; gap: 8px; }
.choice { min-height: 44px; margin: 0; padding: 8px 12px; border: 1px solid var(--line); border-radius: 8px; background: #fff; color: var(--ink); font-size: 14px; cursor: pointer; }
.choice::after { border: 0; }
.choice.active { border-color: var(--green); background: #e6f1e9; font-weight: 650; }
.preview-card { width: min(100%, 980px); height: min(90vh, 900px); display: flex; flex-direction: column; gap: 8px; padding: 16px; }
.preview-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.preview-text { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.preview-viewer { flex: 1; min-height: 0; border: 1px solid var(--line); border-radius: 8px; }
.drag-ghost { position: fixed; z-index: 30; margin: 8px 0 0 8px; padding: 6px 12px; border-radius: 8px; background: var(--green); color: #fff; font-size: 13px; font-weight: 700; pointer-events: none; box-shadow: 3px 3px 0 0 rgba(23, 59, 46, .35), 6px 6px 0 0 rgba(23, 59, 46, .18); }
@media (max-width: 899px) {
  .head { padding: 16px; }
  .section { padding: 12px; }
  .group-actions { width: 100%; }
}
</style>
