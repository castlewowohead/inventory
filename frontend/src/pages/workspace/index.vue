<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import InvoiceList from '../../features/invoices/InvoiceList.vue'
import InvoiceUpload from '../../features/invoices/InvoiceUpload.vue'
import InvoiceDetail from '../../features/invoices/InvoiceDetail.vue'
import IntakeBatchUpload from '../../features/invoices/IntakeBatchUpload.vue'
import IntakeOrganizer from '../../features/invoices/IntakeOrganizer.vue'
import IntakeLedger from '../../features/invoices/IntakeLedger.vue'
import IngredientList from '../../features/ingredients/IngredientList.vue'
import PriceWorkspace from '../../features/prices/PriceWorkspace.vue'
import { ApiError, errorMessage, getCategories, getCurrentIntakeBatch, getHealth, getIntakeBatch } from '../../shared/api'
import { confirmLeave, pendingLeaveReason, watchPageUnload } from '../../shared/platform/leaveGuard'
import { isCancelledPick, pickIntakeFiles, type PickedFile } from '../../shared/platform/filePicker'
import { countLocalPages } from '../../shared/platform/documentPreview'
import {
  onWorkspaceHistoryChange,
  readWorkspaceLocation,
  writeWorkspaceLocation,
  type WorkspaceLocation
} from '../../shared/platform/workspaceNavigation'

const location = ref<WorkspaceLocation>(readWorkspaceLocation())
const boardRoutePending = ref(location.value.view === 'organize' || location.value.view === 'ledger')
const seenOrganize = ref(location.value.view === 'organize')
const seenLedger = ref(location.value.view === 'ledger')
watch(() => location.value.view, (view) => {
  if (view === 'organize') seenOrganize.value = true
  if (view === 'ledger') seenLedger.value = true
  if (view === 'home') void readCurrentBatch()
})
const isReview = computed(() => location.value.view === 'review')
const serviceState = ref<'checking' | 'ready' | 'error'>('checking')
const serviceText = ref('正在连接…')
const serviceError = ref('')
const invoiceList = ref<{ refresh: () => Promise<void>; showCompleted: () => void; showUpload: () => void } | null>(null)
const uploadFiles = ref<PickedFile[]>([])
const batchFiles = ref<PickedFile[]>([])
const currentBatchId = ref<number | null>(null)
const reviewQueue = ref<number[]>([])
/**
 * Where the fill page was entered from while its source batch is still open.
 *
 * 第 11 节 ⑤: the whole batch should be finishable from the organizing board,
 * so a draft opened from it must not drop the user in the invoice database on
 * the way out.
 */
const reviewOrigin = ref<'invoices' | 'organize' | 'ledger'>('invoices')
const uploadPickError = ref('')
let stopHistory = () => {}
let stopUnload = () => {}

function go(next: WorkspaceLocation) {
  location.value = next
  writeWorkspaceLocation(next)
}

/** A finished batch is historical evidence; its classification board is no longer a destination. */
async function openBatchId(batchId: number | null | undefined): Promise<number | null> {
  if (batchId == null) return null
  try { return (await getIntakeBatch(batchId)).status === 'open' ? batchId : null }
  catch { return null }
}

function replaceClosedBoard() {
  const next: WorkspaceLocation = { view: 'invoices', invoiceId: null }
  location.value = next
  writeWorkspaceLocation(next, true)
  void invoiceList.value?.refresh()
}

async function verifyInitialBoard() {
  const initial = location.value
  if (initial.view !== 'organize' && initial.view !== 'ledger') return
  const openId = await openBatchId(initial.batchId)
  if (location.value === initial && openId === null) replaceClosedBoard()
  boardRoutePending.value = false
}

/** Every view switch asks the components first; unsaved input is theirs to keep. */
async function navigate(view: 'invoices' | 'ingredients') {
  if (!await confirmLeave()) return
  if (location.value.view === 'batch') batchFiles.value = []
  go({ view, invoiceId: null, ingredientTab: 'catalog' })
  if (view === 'invoices') void invoiceList.value?.refresh()
}

async function goHome() {
  if (!await confirmLeave()) return
  if (location.value.view === 'upload') uploadFiles.value = []
  if (location.value.view === 'batch') batchFiles.value = []
  go({ view: 'home', invoiceId: null })
}

/**
 * 上传单据：**one** entry for everything (`docs/multifiles.md` 第 11 节 ①).
 *
 * The user should not have to classify their own upload before making it. What
 * decides the route is the material: **one document** (a single photo, or a
 * one-page PDF) has nothing to organize, so it goes straight to the fill page
 * through the existing single-invoice path. Anything else — several files, a
 * multi-page PDF, a spreadsheet — goes to the batch and its organizing board.
 *
 * ⚠️ An unreadable or unknown page count routes to the **batch**, never to the
 * single path: the board can handle one item, while the single path would
 * silently turn a stack of invoices into one.
 */
function openDocumentUpload() {
  uploadPickError.value = ''
  // File dialogs require the original click gesture; no navigation or await first.
  pickIntakeFiles().then(async (files) => {
    if (!files.length) return
    if (await isSingleDocument(files)) {
      uploadFiles.value = files
      go({ view: 'upload', invoiceId: null })
      return
    }
    batchFiles.value = files
    go({ view: 'batch', invoiceId: null, batchId: null })
  }).catch((error) => {
    if (!isCancelledPick(error)) uploadPickError.value = errorMessage(error)
  })
}

async function isSingleDocument(files: PickedFile[]): Promise<boolean> {
  if (files.length !== 1) return false
  const file = files[0]
  // A spreadsheet is rows, not a document: it needs column mapping and grouping.
  if (/\.(csv|xlsx)$/i.test(file.name)) return false
  return await countLocalPages(file.source, file.name, file.contentType) === 1
}

async function resumeBatch() {
  if (!await confirmLeave()) return
  batchFiles.value = []
  go({ view: 'batch', invoiceId: null, batchId: currentBatchId.value })
}

async function openBatchFromLegacy() {
  if (!await confirmLeave()) return
  uploadFiles.value = []
  batchFiles.value = []
  go({ view: 'batch', invoiceId: null, batchId: currentBatchId.value })
}

function batchOpened(id: number) {
  currentBatchId.value = id
  const view = location.value.view
  if ((view === 'batch' || view === 'organize' || view === 'ledger') && location.value.batchId !== id) {
    const next: WorkspaceLocation = { view, invoiceId: null, batchId: id }
    location.value = next
    writeWorkspaceLocation(next, true)
  }
}

/** 上传页 → 整理页。Asks first: the upload queue is local, not on the server. */
async function openOrganize(id: number) {
  if (!await confirmLeave()) return
  batchFiles.value = []
  if (await openBatchId(id) === null) { go({ view: 'invoices', invoiceId: null }); return }
  go({ view: 'organize', invoiceId: null, batchId: id })
}

async function switchIntakeView(view: 'organize' | 'ledger') {
  const batchId = await openBatchId(location.value.batchId ?? currentBatchId.value)
  if (batchId === null) { go({ view: 'invoices', invoiceId: null }); return }
  go({ view, invoiceId: null, batchId })
}

async function backToBatchUpload() {
  if (!await confirmLeave()) return
  go({ view: 'batch', invoiceId: null, batchId: currentBatchId.value })
}

async function readCurrentBatch() {
  try { currentBatchId.value = (await getCurrentIntakeBatch()).id }
  catch (reason) { if (reason instanceof ApiError && reason.status === 404) currentBatchId.value = null }
}

function finishedUpload(id: number) {
  uploadFiles.value = []
  const review: WorkspaceLocation = { view: 'review', invoiceId: id }
  location.value = review
  writeWorkspaceLocation(review, true)
  void invoiceList.value?.refresh()
}

function selectPriceFoodItem(id: number | null) {
  go({ view: 'ingredients', invoiceId: null, foodItemId: id, ingredientTab: 'prices' })
}

async function selectIngredientTab(tab: 'catalog' | 'prices') {
  if (!await confirmLeave()) return
  go({ view: 'ingredients', invoiceId: null, foodItemId: tab === 'prices' ? location.value.foodItemId : null, ingredientTab: tab })
}

async function openReview(id: number, origin: 'invoices' | 'organize' | 'ledger' = 'invoices') {
  if (!await confirmLeave()) return
  reviewQueue.value = [id]
  reviewOrigin.value = origin
  go({ view: 'review', invoiceId: id })
}

async function openReviewSelection(ids: number[]) {
  if (!ids.length || !await confirmLeave()) return
  reviewQueue.value = ids
  reviewOrigin.value = 'organize'
  go({ view: 'review', invoiceId: ids[0] })
}

async function switchReview(id: number) {
  if (location.value.invoiceId === id || !await confirmLeave()) return
  go({ view: 'review', invoiceId: id })
}

async function backToInvoices(batchId: number | null) {
  if (!await confirmLeave()) return
  const openId = await openBatchId(batchId)
  if (openId !== null) {
    go({ view: reviewOrigin.value === 'ledger' ? 'ledger' : 'organize', invoiceId: null, batchId: openId })
    return
  }
  go({ view: 'invoices', invoiceId: null })
  void invoiceList.value?.refresh()
}

/**
 * 一张填完了。`batchId` 是**这张单据自己**的出处（`intake_sources`）。
 *
 * ⚠️ 不能只看 `reviewOrigin`：它是内存里的一个 ref，刷新一下就回到默认的
 * 「单据库」，从单据库点进来接着填的那一张也一样。于是填完一张就被丢回单据库，
 * 而第 11 节 ⑤ 说好的是整批在整理页里走完（2026-11-24 反馈，第 22 节）。
 * 批次是否仍开放以服务端状态为准；已经收口的批次回单据库。
 */
async function completedInvoice(batchId: number | null) {
  const currentIndex = reviewQueue.value.indexOf(location.value.invoiceId ?? -1)
  if (currentIndex >= 0 && currentIndex + 1 < reviewQueue.value.length) {
    go({ view: 'review', invoiceId: reviewQueue.value[currentIndex + 1] })
    return
  }
  reviewQueue.value = []
  const openId = await openBatchId(batchId)
  if (openId !== null) {
    go({ view: reviewOrigin.value === 'ledger' ? 'ledger' : 'organize', invoiceId: null, batchId: openId })
    return
  }
  go({ view: 'invoices', invoiceId: null })
  invoiceList.value?.showCompleted()
}

/**
 * 整理页底下那个「确认完成所有填写」：此次多文件填表结束（第 22 节）。
 *
 * 批次已经在服务端收口，这里重新读一次 `/current`：它于是 404，首页那个
 * 「接着整理」就不再出现。落在单据库的「已完成」：刚填完的那几张都在那里。
 */
async function finishedOrganizing() {
  reviewQueue.value = []
  batchFiles.value = []
  await readCurrentBatch()
  go({ view: 'invoices', invoiceId: null })
  invoiceList.value?.showCompleted()
}

/**
 * Browser back / forward. The URL already changed when this runs, so a user who
 * decides to stay gets the previous view pushed back on.
 */
async function syncHistory() {
  const next = readWorkspaceLocation()
  const current = location.value
  if (next.view === current.view && next.invoiceId === current.invoiceId && (next.batchId ?? null) === (current.batchId ?? null) && (next.foodItemId ?? null) === (current.foodItemId ?? null) && (next.ingredientTab ?? 'catalog') === (current.ingredientTab ?? 'catalog')) return
  if (pendingLeaveReason() && !await confirmLeave()) {
    writeWorkspaceLocation(current)
    return
  }
  if ((next.view === 'organize' || next.view === 'ledger') && await openBatchId(next.batchId) === null) {
    replaceClosedBoard()
    return
  }
  if (current.view === 'batch' && next.view !== 'batch') batchFiles.value = []
  location.value = next
  if (next.view === 'invoices') void invoiceList.value?.refresh()
}

async function checkConnection() {
  serviceState.value = 'checking'
  serviceError.value = ''
  const [health, categories] = await Promise.allSettled([getHealth(), getCategories()])
  const failures: string[] = []
  if (health.status === 'rejected') failures.push(errorMessage(health.reason))
  if (categories.status === 'rejected') failures.push(errorMessage(categories.reason))
  if (failures.length) {
    serviceState.value = 'error'
    serviceText.value = '连接中断'
    serviceError.value = [...new Set(failures)].join('；')
  } else if (categories.status === 'fulfilled') {
    serviceState.value = 'ready'
    serviceText.value = `已连接 · ${categories.value.items.length} 个分类`
  }
}

onMounted(() => {
  location.value = readWorkspaceLocation()
  boardRoutePending.value = location.value.view === 'organize' || location.value.view === 'ledger'
  stopHistory = onWorkspaceHistoryChange(() => { void syncHistory() })
  stopUnload = watchPageUnload()
  void checkConnection()
  void readCurrentBatch()
  void verifyInitialBoard()
})
onUnmounted(() => { stopHistory(); stopUnload() })
</script>

<template>
  <view class="shell" :class="{ 'review-shell': isReview }">
    <view class="main-area">
      <view class="content-wrap">
        <view v-if="location.view !== 'home' && !isReview" class="back-row">
          <button class="back-home" @click="goHome">← 返回主页面</button>
        </view>
        <view v-if="serviceState !== 'ready' || location.view === 'home' || !isReview" class="connection-line" role="status" aria-live="polite">
          <text class="connection-dot" :class="serviceState" aria-hidden="true"></text>
          <text>{{ serviceText }}</text>
          <button v-if="serviceState === 'error'" class="retry" @click="checkConnection">重试</button>
        </view>
        <text v-if="serviceError" class="service-error" role="alert">{{ serviceError }}</text>

        <view v-if="location.view === 'home'" class="home">
          <text class="home-brand">Inventory</text>
          <text class="home-subtitle">采购单据工作台</text>
          <view class="home-actions">
            <button class="home-action primary" @click="openDocumentUpload"><text class="home-action-title">上传单据</text><text class="home-action-description">一次选图片、PDF、CSV 或 XLSX；只有一份单据时直接进入填写</text></button>
            <button v-if="currentBatchId" class="home-action" @click="resumeBatch"><text class="home-action-title">接着整理</text><text class="home-action-description">打开上次的文件批次</text></button>
            <button class="home-action" @click="navigate('invoices')"><text class="home-action-title">单据库</text><text class="home-action-description">查看待处理与已完成单据</text></button>
            <button class="home-action" @click="navigate('ingredients')"><text class="home-action-title">食材</text><text class="home-action-description">维护食材与查看价格</text></button>
          </view>
          <text v-if="uploadPickError" class="upload-pick-error" role="alert">{{ uploadPickError }}</text>
        </view>
        <InvoiceUpload v-if="location.view === 'upload'" mode="create" :initial-files="uploadFiles" :auto-start="true" @uploaded="finishedUpload" @open-existing="finishedUpload" @open-batch="openBatchFromLegacy" @close="goHome" />
        <IntakeBatchUpload v-if="location.view === 'batch'" :batch-id="location.batchId" :initial-files="batchFiles" @opened="batchOpened" @list="navigate('invoices')" @organize="openOrganize" />
        <view v-if="!boardRoutePending && (location.view === 'organize' || location.view === 'ledger')" class="intake-views">
          <IntakeOrganizer v-if="seenOrganize" v-show="location.view === 'organize'" :active="location.view === 'organize'" :batch-id="location.batchId" @opened="batchOpened" @upload="backToBatchUpload" @ledger="switchIntakeView('ledger')" @review="openReviewSelection" @finished="finishedOrganizing" />
          <IntakeLedger v-if="seenLedger" v-show="location.view === 'ledger'" :active="location.view === 'ledger'" :batch-id="location.batchId" @opened="batchOpened" @upload="backToBatchUpload" @cards="switchIntakeView('organize')" @review="openReview($event, 'ledger')" />
        </view>
        <InvoiceList v-show="location.view === 'invoices'" ref="invoiceList" @open="openReview" @open-batch="resumeBatch" />
        <view v-if="location.view === 'review' && reviewQueue.length > 1" class="review-queue">
          <text>本次选中 {{ reviewQueue.length }} 张单据，分别填表</text>
          <button v-for="(id, index) in reviewQueue" :key="id" class="ui-button secondary"
            :class="{ active: location.invoiceId === id }" @click="switchReview(id)">
            第 {{ index + 1 }} 张 · 单据 #{{ id }}
          </button>
        </view>
        <InvoiceDetail v-if="location.view === 'review'" :invoice-id="location.invoiceId" @back="backToInvoices" @saved="invoiceList?.refresh()" @completed="completedInvoice" @open-existing="openReview" />
        <view v-if="location.view === 'ingredients'" class="ingredients-workspace">
          <IngredientList v-if="location.ingredientTab !== 'prices'" @open-prices="selectIngredientTab('prices')" />
          <PriceWorkspace v-else :food-item-id="location.foodItemId ?? null" @select="selectPriceFoodItem" @back="selectIngredientTab('catalog')" />
        </view>
      </view>
    </view>
  </view>
</template>

<style scoped>
.shell { min-height: 100vh; background: var(--canvas); }
.main-area { min-width: 0; flex: 1; }
.content-wrap { max-width: 1440px; margin: 0 auto; padding: 24px 32px 48px; }
.review-shell .content-wrap { max-width: none; padding: 12px 20px 24px; }
.review-queue { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.review-queue .active { border-color: var(--green); color: var(--green); font-weight: 700; }
.back-row { display: flex; justify-content: flex-start; margin-bottom: 8px; }
.back-home { display: inline-flex; align-items: center; flex: 0 0 auto; width: auto; min-height: 44px; margin: 0; padding: 6px 0; border: 0; background: transparent; color: var(--green); font-size: 15px; cursor: pointer; }
.back-home::after { border: 0; }
.home { min-height: min(78vh, 720px); display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 32px 0 80px; text-align: center; }
.home-brand { color: var(--ink); font-size: clamp(36px, 6vw, 64px); font-weight: 750; }
.home-subtitle { margin-top: 8px; color: var(--muted); font-size: 18px; }
.home-actions { width: min(100%, 480px); display: flex; flex-direction: column; gap: 18px; margin-top: 50px; }
.home-action { min-height: 110px; display: flex; flex-direction: column; align-items: flex-start; justify-content: center; gap: 8px; margin: 0; padding: 22px 28px; border: 1px solid var(--line); border-radius: 12px; background: #fff; color: var(--ink); text-align: left; cursor: pointer; }
.home-action.primary { border-color: var(--green); background: var(--green); color: #fff; }
.home-action-title { font-size: 22px; font-weight: 700; }
.home-action-description { font-size: 13px; opacity: .8; }
.upload-pick-error { margin-top: 20px; color: var(--error); font-size: 14px; }
.connection-line { min-height: 28px; display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-bottom: 16px; color: var(--muted); font-size: 12px; }
.connection-dot { width: 8px; height: 8px; flex: none; border-radius: 50%; background: #889c8e; }
.connection-dot.ready { background: var(--green); }
.connection-dot.error { background: var(--error); }
.retry { min-height: 44px; min-width: 44px; margin: 0 0 0 4px; padding: 8px; border: 0; background: transparent; color: var(--green); font-size: 12px; cursor: pointer; }
.retry::after { border: 0; }
.service-error { display: block; margin: -8px 0 16px; color: var(--error); font-size: 13px; text-align: right; }
@media (max-width: 899px) {
  .content-wrap { padding: 12px 16px calc(24px + env(safe-area-inset-bottom)); }
  .review-shell .content-wrap { padding: 12px 16px calc(24px + env(safe-area-inset-bottom)); }
  .connection-line { justify-content: flex-start; min-height: 24px; margin-bottom: 12px; }
  .service-error { margin: -4px 0 12px; text-align: left; }
  .home { min-height: 75vh; padding: 20px 0 60px; }
  .home-actions { margin-top: 32px; }
}
</style>
