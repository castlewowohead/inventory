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
import { ApiError, errorMessage, getCategories, getCurrentIntakeBatch, getHealth } from '../../shared/api'
import { confirmLeave, pendingLeaveReason, watchPageUnload } from '../../shared/platform/leaveGuard'
import { isCancelledPick, pickDocuments, pickIntakeFiles, type PickedFile } from '../../shared/platform/filePicker'
import { MAX_FILES_PER_INVOICE } from '../../features/invoices/uploadQueue'
import {
  onWorkspaceHistoryChange,
  readWorkspaceLocation,
  writeWorkspaceLocation,
  type WorkspaceLocation
} from '../../shared/platform/workspaceNavigation'

const location = ref<WorkspaceLocation>(readWorkspaceLocation())
const seenOrganize = ref(location.value.view === 'organize')
const seenLedger = ref(location.value.view === 'ledger')
watch(() => location.value.view, (view) => {
  if (view === 'organize') seenOrganize.value = true
  if (view === 'ledger') seenLedger.value = true
})
const isReview = computed(() => location.value.view === 'review')
const serviceState = ref<'checking' | 'ready' | 'error'>('checking')
const serviceText = ref('正在连接…')
const serviceError = ref('')
const invoiceList = ref<{ refresh: () => Promise<void>; showCompleted: () => void; showUpload: () => void } | null>(null)
const uploadFiles = ref<PickedFile[]>([])
const batchFiles = ref<PickedFile[]>([])
const currentBatchId = ref<number | null>(null)
const uploadPickError = ref('')
let stopHistory = () => {}
let stopUnload = () => {}

function go(next: WorkspaceLocation) {
  location.value = next
  writeWorkspaceLocation(next)
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

function openUpload() {
  uploadPickError.value = ''
  // File dialogs require the original click gesture; no navigation or await first.
  pickDocuments(MAX_FILES_PER_INVOICE).then((files) => {
    if (!files.length) return
    uploadFiles.value = files
    go({ view: 'upload', invoiceId: null })
  }).catch((error) => {
    if (!isCancelledPick(error)) uploadPickError.value = errorMessage(error)
  })
}

function openBatchUpload() {
  uploadPickError.value = ''
  pickIntakeFiles().then((files) => {
    if (!files.length) return
    batchFiles.value = files
    go({ view: 'batch', invoiceId: null, batchId: null })
  }).catch((reason) => {
    if (!isCancelledPick(reason)) uploadPickError.value = errorMessage(reason)
  })
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
  go({ view: 'organize', invoiceId: null, batchId: id })
}

function switchIntakeView(view: 'organize' | 'ledger') {
  go({ view, invoiceId: null, batchId: location.value.batchId ?? currentBatchId.value })
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

async function openReview(id: number) {
  if (!await confirmLeave()) return
  go({ view: 'review', invoiceId: id })
}

async function backToInvoices() {
  if (!await confirmLeave()) return
  go({ view: 'invoices', invoiceId: null })
  void invoiceList.value?.refresh()
}

function completedInvoice() {
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
  stopHistory = onWorkspaceHistoryChange(() => { void syncHistory() })
  stopUnload = watchPageUnload()
  void checkConnection()
  void readCurrentBatch()
})
onUnmounted(() => { stopHistory(); stopUnload() })
</script>

<template>
  <view class="shell" :class="{ 'review-shell': isReview }">
    <view class="main-area">
      <view class="content-wrap">
        <view v-if="location.view !== 'home'" class="back-row">
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
            <button class="home-action primary" @click="openBatchUpload"><text class="home-action-title">混合文件整理</text><text class="home-action-description">一次选图片、PDF、CSV 或 XLSX</text></button>
            <button v-if="currentBatchId" class="home-action" @click="resumeBatch"><text class="home-action-title">接着整理</text><text class="home-action-description">打开上次的文件批次</text></button>
            <button class="home-action" @click="openUpload"><text class="home-action-title">单张单据上传</text><text class="home-action-description">拍照或选择图片、PDF，直接进入单据审核</text></button>
            <button class="home-action" @click="navigate('invoices')"><text class="home-action-title">单据库</text><text class="home-action-description">查看待处理与已完成单据</text></button>
            <button class="home-action" @click="navigate('ingredients')"><text class="home-action-title">食材</text><text class="home-action-description">维护食材与查看价格</text></button>
          </view>
          <text v-if="uploadPickError" class="upload-pick-error" role="alert">{{ uploadPickError }}</text>
        </view>
        <InvoiceUpload v-if="location.view === 'upload'" mode="create" :initial-files="uploadFiles" :auto-start="true" @uploaded="finishedUpload" @open-existing="finishedUpload" @open-batch="openBatchFromLegacy" @close="goHome" />
        <IntakeBatchUpload v-if="location.view === 'batch'" :batch-id="location.batchId" :initial-files="batchFiles" @opened="batchOpened" @list="navigate('invoices')" @organize="openOrganize" />
        <view v-if="location.view === 'organize' || location.view === 'ledger'" class="intake-views">
          <IntakeOrganizer v-if="seenOrganize" v-show="location.view === 'organize'" :active="location.view === 'organize'" :batch-id="location.batchId" @opened="batchOpened" @upload="backToBatchUpload" @list="navigate('invoices')" @ledger="switchIntakeView('ledger')" @review="openReview" />
          <IntakeLedger v-if="seenLedger" v-show="location.view === 'ledger'" :active="location.view === 'ledger'" :batch-id="location.batchId" @opened="batchOpened" @upload="backToBatchUpload" @cards="switchIntakeView('organize')" @review="openReview" />
        </view>
        <InvoiceList v-show="location.view === 'invoices'" ref="invoiceList" @open="openReview" @open-batch="resumeBatch" />
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
