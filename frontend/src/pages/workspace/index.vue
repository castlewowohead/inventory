<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import InvoiceList from '../../features/invoices/InvoiceList.vue'
import InvoiceUpload from '../../features/invoices/InvoiceUpload.vue'
import InvoiceDetail from '../../features/invoices/InvoiceDetail.vue'
import IngredientList from '../../features/ingredients/IngredientList.vue'
import PriceWorkspace from '../../features/prices/PriceWorkspace.vue'
import { errorMessage, getCategories, getHealth } from '../../shared/api'
import { confirmLeave, pendingLeaveReason, watchPageUnload } from '../../shared/platform/leaveGuard'
import { isCancelledPick, pickDocuments, type PickedFile } from '../../shared/platform/filePicker'
import { MAX_FILES_PER_INVOICE } from '../../features/invoices/uploadQueue'
import {
  onWorkspaceHistoryChange,
  readWorkspaceLocation,
  writeWorkspaceLocation,
  type WorkspaceLocation
} from '../../shared/platform/workspaceNavigation'

const location = ref<WorkspaceLocation>(readWorkspaceLocation())
const isReview = computed(() => location.value.view === 'review')
const serviceState = ref<'checking' | 'ready' | 'error'>('checking')
const serviceText = ref('正在检查接口…')
const serviceError = ref('')
const invoiceList = ref<{ refresh: () => Promise<void>; showCompleted: () => void; showUpload: () => void } | null>(null)
const uploadFiles = ref<PickedFile[]>([])
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
  go({ view, invoiceId: null, ingredientTab: 'catalog' })
  if (view === 'invoices') void invoiceList.value?.refresh()
}

async function goHome() {
  if (!await confirmLeave()) return
  if (location.value.view === 'upload') uploadFiles.value = []
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
  if (next.view === current.view && next.invoiceId === current.invoiceId && (next.foodItemId ?? null) === (current.foodItemId ?? null) && (next.ingredientTab ?? 'catalog') === (current.ingredientTab ?? 'catalog')) return
  if (pendingLeaveReason() && !await confirmLeave()) {
    writeWorkspaceLocation(current)
    return
  }
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
    serviceText.value = '接口连接异常'
    serviceError.value = [...new Set(failures)].join('；')
  } else if (categories.status === 'fulfilled') {
    serviceState.value = 'ready'
    serviceText.value = `接口已连接 · ${categories.value.items.length} 个分类`
  }
}

onMounted(() => {
  location.value = readWorkspaceLocation()
  stopHistory = onWorkspaceHistoryChange(() => { void syncHistory() })
  stopUnload = watchPageUnload()
  void checkConnection()
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
            <button class="home-action primary" @click="openUpload"><text class="home-action-title">上传单据</text><text class="home-action-description">拍照或选择图片、PDF</text></button>
            <button class="home-action" @click="navigate('invoices')"><text class="home-action-title">单据库</text><text class="home-action-description">查看待处理与已完成单据</text></button>
            <button class="home-action" @click="navigate('ingredients')"><text class="home-action-title">食材</text><text class="home-action-description">维护食材与查看价格</text></button>
          </view>
          <text v-if="uploadPickError" class="upload-pick-error" role="alert">{{ uploadPickError }}</text>
        </view>
        <InvoiceUpload v-if="location.view === 'upload'" mode="create" :initial-files="uploadFiles" :auto-start="true" @uploaded="finishedUpload" @open-existing="finishedUpload" @close="goHome" />
        <InvoiceList v-show="location.view === 'invoices'" ref="invoiceList" @open="openReview" />
        <InvoiceDetail v-if="location.view === 'review'" :invoice-id="location.invoiceId" @back="backToInvoices" @saved="invoiceList?.refresh()" @completed="completedInvoice" @open-existing="openReview" />
        <view v-if="location.view === 'ingredients'" class="ingredients-workspace">
          <view class="ingredient-tabs" aria-label="食材页面">
            <button :class="{ active: location.ingredientTab !== 'prices' }" :aria-current="location.ingredientTab !== 'prices' ? 'page' : undefined" @click="selectIngredientTab('catalog')">食材</button>
            <button :class="{ active: location.ingredientTab === 'prices' }" :aria-current="location.ingredientTab === 'prices' ? 'page' : undefined" @click="selectIngredientTab('prices')">价格</button>
          </view>
          <IngredientList v-if="location.ingredientTab !== 'prices'" />
          <PriceWorkspace v-else :food-item-id="location.foodItemId ?? null" @select="selectPriceFoodItem" />
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
.back-row { margin-bottom: 8px; }
.back-home { min-height: 44px; margin: 0; padding: 6px 0; border: 0; background: transparent; color: var(--green); font-size: 15px; cursor: pointer; }
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
.ingredient-tabs { display: flex; gap: 24px; margin-bottom: 22px; border-bottom: 1px solid var(--line); }
.ingredient-tabs button { min-height: 48px; margin: 0; padding: 8px 4px; border: 0; border-bottom: 3px solid transparent; border-radius: 0; background: transparent; color: var(--muted); font-size: 16px; cursor: pointer; }
.ingredient-tabs button::after { border: 0; }
.ingredient-tabs button.active { border-color: var(--green); color: var(--ink); font-weight: 700; }
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
