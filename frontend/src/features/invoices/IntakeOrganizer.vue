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
 * ### Handing a confirmed group to review (M12)
 *
 * 整理 ends here; 填写 and 逐行确认 do not live on this page. The 送入审核 button
 * posts the M08 command for the **explicitly selected** contents of one
 * confirmed group — never for "the group", because a 日期/商家 group may hold
 * several real invoices and a draft made of all of them would be a fake single
 * invoice. A handed-off content stops accepting moves; the card says so instead
 * of waiting for the server's 409.
 *
 * Dragging is the desktop shortcut only. Every move is reachable by keyboard
 * and by single taps, which is what makes the narrow layout and W3C G219 work.
 *
 * ### What the board is, and is not
 *
 * The right-hand side is **derived**: `GET /groups` groups the human decisions,
 * so a group exists because somebody put a content there. The board therefore
 * never shows a group the user has not created, and a suggestion stays on its
 * card in 待归类 instead of being filed into the group it hints at — the plan
 * allows showing high-confidence candidates in their group, but not at the cost
 * of blurring 系统建议 and 人工确认, and a card that moved by itself is exactly
 * that blur. 整理 is not 入账: nothing here writes a purchase.
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
  ApiError, errorMessage, getIntakeBatch, getIntakeGroupItems, getIntakeGroups,
  getCurrentIntakeBatch, handOffIntakeItems, moveIntakeItems, processIntakeBatch,
  undoIntakeMove,
  type IntakeBatch, type IntakeGroup, type IntakeItem, type IntakeSource
} from '../../shared/api'
import { isNarrowViewport, watchViewport } from '../../shared/platform/viewport'
import { boardDragSupported, cancelBoardDrag, startBoardDrag } from '../../shared/platform/dragBoard'
import IntakeCard from './IntakeCard.vue'
import OriginalDocumentViewer from './OriginalDocumentViewer.vue'
import {
  alreadyThere, bucketKey, buildBoard, cardLocation, dropKey, handoffProblem, isHandedOff,
  isIsoDate, monthDayLabel, parseDropKey, recognitionNote, suggestionsOf, yearLabel,
  zoneAction, zoneLabel, zoneTarget,
  type BoardTarget, type DropZone
} from './intakeBoard'

const props = withDefaults(defineProps<{ batchId?: number | null; active?: boolean }>(), { batchId: null, active: true })
const emit = defineEmits<{
  upload: []; list: []; ledger: []; opened: [id: number]; review: [invoiceId: number]
}>()

/** How many cards one page of a bucket holds. 加载更多 asks for the next page. */
const PAGE_SIZE = 20
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
const groups = ref<IntakeGroup[]>([])
const buckets = ref<Record<string, Bucket>>({})
const expanded = ref<string[]>([])
const selection = ref<IntakeItem[]>([])
const loading = ref(true)
const error = ref('')
const conflict = ref('')
const notice = ref('')
const moving = ref(false)
const processing = ref(false)
const lastMove = ref<{ id: number; text: string } | null>(null)
const failed = ref<{ key: string; zone: DropZone; items: IntakeItem[] } | null>(null)
const preview = ref<IntakeItem | null>(null)
const cardMenu = ref<IntakeItem | null>(null)
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
const handing = ref(false)
const narrow = ref(isNarrowViewport())
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
const pendingBucket = computed(() => buckets.value[bucketKey(PENDING)] ?? null)
const contentTotal = computed(() => groups.value.reduce((sum, group) => sum + group.item_count, 0))
const groupedTotal = computed(() => contentTotal.value - board.value.pendingCount)
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
    expanded.value = expanded.value.filter((value) => value !== key)
    return
  }
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

async function loadGroups(batchId: number) {
  const collected: IntakeGroup[] = []
  for (;;) {
    const page = await getIntakeGroups(batchId, GROUP_PAGE, collected.length)
    collected.push(...page.groups)
    if (!page.groups.length || collected.length >= page.total || collected.length >= MAX_GROUPS) break
  }
  groups.value = collected
}

/** Re-reads only what is on screen: 待归类 plus the groups the user opened. */
async function reloadVisible() {
  const live = new Set(groups.value.map((group) =>
    bucketKey({ purchaseDate: group.purchase_date, sourceName: group.source_name })))
  expanded.value = expanded.value.filter((key) => live.has(key))
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
    const [current] = await Promise.all([getIntakeBatch(batch.value.id), loadGroups(batch.value.id)])
    if (stopped) return
    batch.value = current
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
    await loadGroups(opened.id)
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

async function dispatchMove(zone: DropZone, items: IntakeItem[], reuseKey?: string) {
  if (!batch.value || moving.value) return
  if (!canMove.value) {
    conflict.value = '这个批次已经收口或作废，不能再移动内容。'
    return
  }
  // Already in review: say why instead of letting the server refuse the batch.
  const locked = items.filter(isHandedOff)
  const movable = items.filter((item) => !isHandedOff(item))
  if (!movable.length) {
    conflict.value = locked.length === 1
      ? '这一项已经进入单据审核，整理页不能再移动它；要改日期或商家，请在审核里修改分类。'
      : `选中的 ${locked.length} 项都已经进入单据审核，整理页不能再移动它们；要改日期或商家，请在审核里修改分类。`
    return
  }
  const pending = movable.filter((item) => !alreadyThere(item, zone))
  if (!pending.length) {
    notice.value = `选中的内容已经在 ${zoneLabel(zone)}，这一步没有改动任何内容。`
    return
  }
  const action = zoneAction(zone)
  const target = zoneTarget(zone)
  const key = reuseKey ?? nextRequestKey(action)
  moving.value = true
  notice.value = ''
  try {
    const result = await moveIntakeItems(batch.value.id, {
      request_key: key,
      action,
      items: pending.map((item) => ({ id: item.id, version: item.version })),
      ...(action === 'clear' ? {} : { purchase_date: target.purchaseDate as string }),
      ...(action === 'group' ? { source_name: target.sourceName as string } : {})
    })
    const moved = new Set(result.items.map((item) => item.id))
    selection.value = selection.value.filter((item) => !moved.has(item.id))
    const skipped = locked.length ? `，另有 ${locked.length} 项已送审没有移动` : ''
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
 * The selected contents that belong to this confirmed group.
 *
 * Handing off takes **what the user picked**, not the group: one group can hold
 * several real invoices, so the page must not decide where one invoice ends.
 */
function selectedIn(target: BoardTarget): IntakeItem[] {
  return selection.value.filter((item) =>
    item.decided_purchase_date === target.purchaseDate
    && item.decided_source_name === target.sourceName)
}

function handoffBlocked(target: BoardTarget): string {
  return handoffProblem(selectedIn(target))
}

async function handOff(target: BoardTarget) {
  if (!batch.value || handing.value || moving.value) return
  const items = selectedIn(target)
  const problem = handoffProblem(items)
  if (problem) { conflict.value = problem; return }
  handing.value = true
  notice.value = ''
  try {
    const result = await handOffIntakeItems(
      batch.value.id, items.map((item) => ({ id: item.id, version: item.version })))
    conflict.value = ''
    error.value = ''
    lastMove.value = null
    selection.value = []
    notice.value = result.created
      ? `已把 ${items.length} 项送入单据 #${result.invoice_id} 的审核，接下来在审核里逐行填写并确认。`
      : `这些内容已经在单据 #${result.invoice_id} 的审核里，没有重复建单。`
    await refresh()
    emit('review', result.invoice_id)
  } catch (reason) {
    if (reason instanceof ApiError && reason.status === 409) {
      conflict.value = reason.message
      await refresh()
    } else error.value = errorMessage(reason)
  } finally {
    handing.value = false
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

function sendToPending(items: IntakeItem[]) {
  cardMenu.value = null
  if (items.length) void dispatchMove(PENDING_ZONE, items)
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
      <text class="title">按日期和商家整理{{ batch ? ` · 批次 #${batch.id}` : '' }}</text>
      <text class="muted">
        同一天、同一商家的内容放进一组。整理只是归类，不会产生采购记录；填写和逐行确认仍在单据审核里完成。
      </text>
      <text class="muted">
        一组里可能有几张真正的发票，所以「送入审核」送的是你选中的那几项，不是整组：选中属于同一组的页（或表格行），再点那一组的「送入审核」。
      </text>
      <view class="actions">
        <button class="ui-button secondary" @click="emit('upload')">返回上传</button>
        <button class="ui-button secondary" @click="emit('list')">单据列表</button>
        <button v-if="!narrow" class="ui-button secondary" @click="emit('ledger')">财务表格</button>
        <button class="ui-button secondary" :disabled="loading || moving" @click="refresh(true)">刷新</button>
        <button v-if="lastMove" class="ui-button secondary" :disabled="moving" @click="undoLast">撤销上一步</button>
      </view>
      <text v-if="batch" class="muted counts">
        待归类 {{ board.pendingCount }} 项 · 已归类 {{ groupedTotal }} 项 · 共 {{ contentTotal }} 项
      </text>
      <text v-if="batch && !canMove" class="warn-text">这个批次已经结束，只能查看，不能再移动内容。</text>
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
              :selected="selectedIds.has(item.id)" :draggable="canDrag && canMove"
              @toggle="toggle(item)" @preview="openPreview(item)" @menu="openCardMenu(item)"
              @drag="beginDrag(item, $event)" />
            <button v-if="pendingBucket.items.length < pendingBucket.total" class="ui-button secondary"
              :disabled="pendingBucket.loading" @click="loadBucket(PENDING, true)">
              加载更多（还有 {{ pendingBucket.total - pendingBucket.items.length }} 项）
            </button>
          </view>
        </view>

        <view v-if="!narrow || pane === 'board'" class="column groups">
          <view v-if="!board.dated.length && !board.undated.length" class="ui-card empty">
            <text>还没有任何日期分组。</text>
            <text class="muted">选中待归类的内容，再用「移到…」填一个采购日期，就会出现这一天的分组。</text>
            <button class="ui-button" :disabled="!selection.length" @click="openPanel([...selection])">移到…</button>
          </view>
          <view v-for="section in board.dated" :key="section.purchaseDate" class="ui-card section">
            <view class="section-head" :class="zoneClass({ kind: 'date', purchaseDate: section.purchaseDate })"
              :data-drop-key="dropKey({ kind: 'date', purchaseDate: section.purchaseDate })">
              <view class="section-title-wrap">
                <text class="date-label">{{ monthDayLabel(section.purchaseDate) }}</text>
                <text class="muted">{{ yearLabel(section.purchaseDate) }} · {{ section.itemCount }} 项</text>
              </view>
              <button class="ui-button secondary small" :disabled="!selection.length || moving || !canMove"
                @click="moveSelected({ kind: 'date', purchaseDate: section.purchaseDate })">
                只定这一天（{{ selection.length }}）
              </button>
            </view>
            <view v-for="group in section.groups" :key="group.sourceName ?? 'unknown'" class="group"
              :class="zoneClass({ kind: 'group', purchaseDate: section.purchaseDate, sourceName: group.sourceName })"
              :data-drop-key="dropKey({ kind: 'group', purchaseDate: section.purchaseDate, sourceName: group.sourceName })">
              <view class="group-head">
                <text class="group-name" :class="{ unknown: group.sourceName === null }">
                  {{ group.sourceName ?? '商家待定' }} · {{ group.itemCount }} 项
                </text>
                <view class="group-actions">
                  <button class="ui-button secondary small" :disabled="!selection.length || moving || !canMove"
                    @click="moveSelected({ kind: 'group', purchaseDate: section.purchaseDate, sourceName: group.sourceName })">
                    移入所选（{{ selection.length }}）
                  </button>
                  <button v-if="group.sourceName !== null" class="ui-button small"
                    :disabled="handing || moving || !canMove
                      || handoffBlocked({ purchaseDate: section.purchaseDate, sourceName: group.sourceName }) !== ''"
                    :title="handoffBlocked({ purchaseDate: section.purchaseDate, sourceName: group.sourceName })"
                    @click="handOff({ purchaseDate: section.purchaseDate, sourceName: group.sourceName })">
                    送入审核（{{ selectedIn({ purchaseDate: section.purchaseDate, sourceName: group.sourceName }).length }}）
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
                  :key="item.id" :item="item" :selected="selectedIds.has(item.id)" :draggable="canDrag && canMove"
                  @toggle="toggle(item)" @preview="openPreview(item)" @menu="openCardMenu(item)"
                  @drag="beginDrag(item, $event)" />
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
                <button class="ui-button ghost small" @click="toggleExpanded(group)">
                  {{ isExpanded(group) ? '收起' : '查看内容' }}
                </button>
              </view>
              <view v-if="isExpanded(group)" class="cards">
                <IntakeCard v-for="item in buckets[bucketKey(group)]?.items ?? []" :key="item.id" :item="item"
                  :selected="selectedIds.has(item.id)" :draggable="canDrag && canMove"
                  @toggle="toggle(item)" @preview="openPreview(item)" @menu="openCardMenu(item)"
                  @drag="beginDrag(item, $event)" />
              </view>
            </view>
          </view>
        </view>
      </view>
    </template>

    <view v-if="selection.length" class="select-bar">
      <text class="select-count">已选 {{ selection.length }} 项</text>
      <view class="select-actions">
        <button class="ui-button" :disabled="moving || !canMove" @click="openPanel([...selection])">移到…</button>
        <button class="ui-button secondary" :disabled="moving || !canMove" @click="sendToPending([...selection])">移回待归类</button>
        <button class="ui-button secondary" @click="clearSelection">清空选择</button>
      </view>
    </view>

    <view v-if="cardMenu" class="overlay" @click.self="cardMenu = null">
      <view class="ui-card sheet">
        <text class="sheet-title">{{ cardMenu.original_name }}</text>
        <text class="muted">{{ cardLocation(cardMenu) }}</text>
        <button class="ui-button secondary" @click="menuPreview">看原件</button>
        <button class="ui-button" :disabled="!canMove" @click="menuPanel">把这一张移到…</button>
        <button class="ui-button secondary" :disabled="!canMove" @click="menuPending">移回待归类</button>
        <button class="ui-button secondary" @click="menuToggle">
          {{ selectedIds.has(cardMenu.id) ? '取消选中' : '选中这一张' }}
        </button>
        <button class="ui-button ghost" @click="cardMenu = null">关闭</button>
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
        <input v-if="panel.dateMode === 'type'" v-model="panel.typedDate" class="ui-input" placeholder="YYYY-MM-DD"
          aria-label="采购日期" />
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
.title { font-size: 21px; font-weight: 700; }
.muted { color: var(--muted); font-size: 13px; line-height: 1.6; }
.counts { font-size: 13px; font-weight: 650; }
.actions { display: flex; flex-wrap: wrap; gap: 10px; }
.warn-text { color: var(--warning); font-size: 13px; }
.notice { display: flex; flex-direction: column; gap: 8px; padding: 14px 18px; }
.notice-title { font-weight: 650; }
.notice.undo { flex-direction: row; align-items: center; justify-content: space-between; gap: 12px; border-color: var(--green); background: #f3f8f3; }
.notice.conflict { border-color: var(--error); }
.conflict-text { color: var(--error); font-size: 14px; line-height: 1.6; }
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
