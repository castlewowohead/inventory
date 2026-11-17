<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ApiError, deleteFoodItem, errorMessage, getCategories, getFoodItem, getFoodItems } from '../../shared/api'
import type { Category, CategoryRef, FoodItem, MergePlan } from '../../shared/api'
import IngredientForm from './IngredientForm.vue'
import CategoryManager from './CategoryManager.vue'
import IngredientPicker from './IngredientPicker.vue'
import IngredientMerge from './IngredientMerge.vue'

const PAGE_SIZE = 20
const categories = ref<Category[]>([])
const categoryState = ref<'loading' | 'ready' | 'error'>('loading')
const categoryError = ref('')
const keyword = ref('')
const appliedKeyword = ref('')
const categoryId = ref<number | null>(null)
const neverPurchased = ref(false)
const showCategoryChoices = ref(false)
const items = ref<FoodItem[]>([])
const total = ref(0)
const nextOffset = ref(0)
const listState = ref<'loading' | 'ready' | 'error'>('loading')
const listError = ref('')
const loadingMore = ref(false)
const moreError = ref('')
const selectedId = ref<number | null>(null)
const detail = ref<FoodItem | null>(null)
const detailState = ref<'idle' | 'loading' | 'ready' | 'error'>('idle')
const detailError = ref('')
const panel = ref<'create' | 'edit' | 'categories' | 'picker' | 'merge' | null>(null)
const mergeResult = ref('')
const selectedFromPicker = ref<FoodItem | null>(null)
const pickerError = ref('')
const confirmDelete = ref(false)
const deleting = ref(false)
const deleteError = ref('')
let searchTimer: ReturnType<typeof setTimeout> | null = null
let listGeneration = 0
let detailGeneration = 0
let disposed = false

const selectedCategoryName = computed(() => categories.value.find(category => category.id === categoryId.value)?.name_zh
  || categories.value.find(category => category.id === categoryId.value)?.name || '全部分类')
const hasMore = computed(() => nextOffset.value < total.value)

function categoryDisplay(category: CategoryRef) {
  return category.name_zh ? `${category.name_zh} / ${category.name}` : category.name
}

async function loadCategories() {
  categoryState.value = 'loading'
  categoryError.value = ''
  try {
    categories.value = (await getCategories()).items
    if (!disposed) categoryState.value = 'ready'
  } catch (error) {
    if (!disposed) {
      categoryState.value = 'error'
      categoryError.value = errorMessage(error)
    }
  }
}

async function refreshCategories() {
  await loadCategories()
  if (categoryId.value !== null && !categories.value.some(category => category.id === categoryId.value)) {
    categoryId.value = null
  }
  void loadFirstPage()
}

function queryFor(offset: number) {
  const params = new URLSearchParams()
  if (appliedKeyword.value.trim()) params.set('q', appliedKeyword.value.trim())
  if (categoryId.value !== null) params.set('category_id', String(categoryId.value))
  if (neverPurchased.value) params.set('never_purchased', 'true')
  params.set('limit', String(PAGE_SIZE))
  params.set('offset', String(offset))
  return `?${params.toString()}`
}

async function loadFirstPage() {
  const generation = ++listGeneration
  items.value = []
  total.value = 0
  nextOffset.value = 0
  listError.value = ''
  moreError.value = ''
  loadingMore.value = false
  listState.value = 'loading'
  try {
    const page = await getFoodItems(queryFor(0))
    if (disposed || generation !== listGeneration) return
    items.value = page.items
    total.value = page.total
    nextOffset.value = page.offset + page.items.length
    listState.value = 'ready'
  } catch (error) {
    if (disposed || generation !== listGeneration) return
    listError.value = errorMessage(error)
    listState.value = 'error'
  }
}

async function loadMore() {
  if (loadingMore.value || listState.value !== 'ready' || !hasMore.value) return
  const generation = listGeneration
  const offset = nextOffset.value
  loadingMore.value = true
  moreError.value = ''
  try {
    const page = await getFoodItems(queryFor(offset))
    if (disposed || generation !== listGeneration) return
    const known = new Set(items.value.map(item => item.id))
    items.value = [...items.value, ...page.items.filter(item => !known.has(item.id))]
    total.value = page.total
    nextOffset.value = page.offset + page.items.length
    if (page.items.length === 0) nextOffset.value = total.value
  } catch (error) {
    if (!disposed && generation === listGeneration) moreError.value = errorMessage(error)
  } finally {
    if (!disposed && generation === listGeneration) loadingMore.value = false
  }
}

function onKeywordInput(event: Event) {
  const inputEvent = event as Event & { detail?: { value: string } }
  keyword.value = inputEvent.detail?.value ?? (event.target as HTMLInputElement | null)?.value ?? ''
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    appliedKeyword.value = keyword.value
    void loadFirstPage()
  }, 300)
}

function chooseCategory(id: number | null) {
  showCategoryChoices.value = false
  if (categoryId.value === id) return
  categoryId.value = id
  if (searchTimer) {
    clearTimeout(searchTimer)
    searchTimer = null
  }
  appliedKeyword.value = keyword.value
  void loadFirstPage()
}

function toggleNeverPurchased() {
  neverPurchased.value = !neverPurchased.value
  void loadFirstPage()
}

async function openDetail(id: number) {
  const generation = ++detailGeneration
  selectedId.value = id
  detail.value = null
  detailError.value = ''
  detailState.value = 'loading'
  confirmDelete.value = false
  deleteError.value = ''
  try {
    const response = await getFoodItem(id)
    if (disposed || generation !== detailGeneration) return
    detail.value = response
    detailState.value = 'ready'
  } catch (error) {
    if (disposed || generation !== detailGeneration) return
    detailError.value = errorMessage(error)
    detailState.value = 'error'
  }
}

function closeDetail() {
  ++detailGeneration
  selectedId.value = null
  detail.value = null
  detailState.value = 'idle'
}

function openPanel(next: 'create' | 'edit' | 'categories' | 'picker' | 'merge') {
  panel.value = next
  showCategoryChoices.value = false
}

function onMerged(plan: MergePlan) {
  mergeResult.value = `已合并：搬动采购记录 ${plan.moved_purchase_count} 条、发票行 ${plan.moved_invoice_line_count} 条、箱规 ${plan.moved_pack_spec_count} 条。`
  void loadFirstPage()
}

function closePanel() {
  const wasMerge = panel.value === 'merge'
  panel.value = null
  if (wasMerge && detail.value) void openDetail(detail.value.id)
}

function onSaved(saved: FoodItem) {
  panel.value = null
  void openDetail(saved.id)
  void loadFirstPage()
}

function onOpenExisting(id: number) {
  panel.value = null
  void openDetail(id)
}

async function onPickerSelect(id: number) {
  panel.value = null
  pickerError.value = ''
  try {
    selectedFromPicker.value = await getFoodItem(id)
  } catch (error) {
    selectedFromPicker.value = null
    pickerError.value = errorMessage(error)
  }
}

async function removeDetail() {
  if (!detail.value || deleting.value) return
  deleting.value = true
  deleteError.value = ''
  try {
    await deleteFoodItem(detail.value.id)
    closeDetail()
    void loadFirstPage()
  } catch (error) {
    deleteError.value = error instanceof ApiError && error.status === 409
      ? `这项食材已被采购记录或单据引用，不能删除。${error.message}`
      : errorMessage(error)
    confirmDelete.value = false
  } finally {
    deleting.value = false
  }
}

onMounted(() => {
  void loadCategories()
  void loadFirstPage()
})
onUnmounted(() => {
  disposed = true
  ++listGeneration
  ++detailGeneration
  if (searchTimer) clearTimeout(searchTimer)
})
</script>

<template>
  <view class="ingredients-layout">
    <view class="page-heading">
      <view>
        <text class="eyebrow">LIBRARY / 食材</text>
        <text class="page-title">食材</text>
        <text class="muted heading-note">查看和维护标准食材</text>
      </view>
      <button class="ui-button" :disabled="categoryState !== 'ready'" @click="openPanel('create')">新增食材</button>
    </view>

    <view class="filters ui-card">
      <view class="filter-field">
        <text class="filter-label">搜索食材</text>
        <input class="ui-input" :value="keyword" placeholder="搜索英文名、中文名或规格" aria-label="搜索食材" @input="onKeywordInput" />
      </view>
      <view class="filter-field category-field">
        <text class="filter-label">分类</text>
        <button class="ui-input picker-value" :disabled="categoryState !== 'ready'" :aria-expanded="showCategoryChoices" aria-label="筛选分类" @click="showCategoryChoices = !showCategoryChoices">{{ selectedCategoryName }} <text aria-hidden="true">⌄</text></button>
        <view v-if="showCategoryChoices" class="category-options ui-card">
          <button class="category-option" :aria-current="categoryId === null ? 'true' : undefined" @click="chooseCategory(null)">全部分类</button>
          <button v-for="category in categories" :key="category.id" class="category-option" :aria-current="category.id === categoryId ? 'true' : undefined" @click="chooseCategory(category.id)">{{ category.name_zh || category.name }}</button>
        </view>
      </view>
      <button class="never-filter" :aria-pressed="neverPurchased" @click="toggleNeverPurchased">{{ neverPurchased ? '☑' : '□' }} 从没买过</button>
    </view>
    <text class="filter-help">已作废的采购记录仍算买过；草稿行也可能引用筛出的食材，所以筛选结果不保证可删除。</text>
    <view v-if="mergeResult" class="selection-note" role="status">{{ mergeResult }}</view>
    <view v-if="categoryState === 'loading'" class="minor-status" role="status">正在读取分类…</view>
    <view v-else-if="categoryState === 'error'" class="minor-status error-state" role="alert">
      分类读取失败：{{ categoryError }} <button class="text-button" @click="loadCategories">重试分类</button>
    </view>
    <view class="local-actions">
      <button class="text-button" @click="openPanel('picker')">选择食材</button>
      <button class="text-button" :disabled="categoryState !== 'ready'" @click="openPanel('categories')">分类管理</button>
    </view>
    <view v-if="selectedFromPicker" class="selection-note" role="status">已选择：{{ selectedFromPicker.name }}（ID {{ selectedFromPicker.id }}）</view>
    <view v-if="pickerError" class="error-state" role="alert">选择结果读取失败：{{ pickerError }}</view>

    <view class="ui-card list-card">
      <view class="list-head"><text>食材名称</text><text>分类</text><text>基础单位</text></view>
      <view v-if="listState === 'loading'" class="state-panel" role="status">正在读取食材…</view>
      <view v-else-if="listState === 'error'" class="state-panel error-state" role="alert">
        <text>食材读取失败：{{ listError }}</text>
        <button class="ui-button secondary" @click="loadFirstPage">重试</button>
      </view>
      <view v-else-if="items.length === 0" class="state-panel" role="status">没有符合条件的食材。可换个关键词、分类或筛选再试。</view>
      <template v-else>
        <button v-for="item in items" :key="item.id" class="list-row" :aria-label="`查看 ${item.name} 的详情`" @click="openDetail(item.id)">
          <view class="name-cell">
            <text class="item-title">{{ item.name }}</text>
            <text v-if="item.name_zh" class="item-secondary">{{ item.name_zh }}</text>
            <text v-if="item.specification" class="item-secondary">规格：{{ item.specification }}</text>
          </view>
          <text class="cell-text"><text class="mobile-label">分类 · </text>{{ categoryDisplay(item.category) }}</text>
          <text class="cell-text"><text class="mobile-label">基础单位 · </text>{{ item.base_unit }}</text>
        </button>
      </template>
    </view>

    <view v-if="listState === 'ready' && items.length" class="pagination" role="status">
      <text>已显示 {{ items.length }} / {{ total }} 种食材</text>
      <button v-if="hasMore" class="ui-button secondary" :disabled="loadingMore" @click="loadMore">{{ loadingMore ? '正在加载…' : '加载更多' }}</button>
      <view v-if="moreError" class="error-state" role="alert">加载更多失败：{{ moreError }}；可重试。</view>
    </view>

    <view v-if="selectedId !== null" class="detail-overlay" @click.self="closeDetail">
      <view class="detail-panel ui-card" role="dialog" aria-modal="true" aria-label="食材详情">
        <view class="detail-top"><text class="eyebrow">食材详情</text><button class="text-button" @click="closeDetail">关闭</button></view>
        <view v-if="detailState === 'loading'" role="status">正在读取详情…</view>
        <view v-else-if="detailState === 'error'" class="error-state" role="alert">
          <text>详情读取失败：{{ detailError }}</text>
          <button class="ui-button secondary" @click="openDetail(selectedId)">重试</button>
        </view>
        <view v-else-if="detail" class="detail-fields">
          <text class="detail-name">{{ detail.name }}</text>
          <view><text class="detail-label">中文名</text><text>{{ detail.name_zh || '未填写' }}</text></view>
          <view><text class="detail-label">分类</text><text>{{ categoryDisplay(detail.category) }}</text></view>
          <view><text class="detail-label">基础单位</text><text>{{ detail.base_unit }}</text></view>
          <view><text class="detail-label">规格</text><text>{{ detail.specification || '未填写' }}</text></view>
          <view class="detail-actions">
            <button class="ui-button secondary" @click="openPanel('edit')">编辑食材</button>
            <button class="ui-button secondary" @click="openPanel('merge')">合并重复食材</button>
            <button class="ui-button ghost danger" @click="confirmDelete = true">删除食材</button>
          </view>
          <text v-if="deleteError" class="error-state" role="alert">{{ deleteError }}</text>
          <view v-if="confirmDelete" class="delete-check">
            <text>确定删除「{{ detail.name }}」？已被单据或采购记录引用的食材无法删除。</text>
            <view class="detail-actions"><button class="ui-button secondary" @click="confirmDelete = false">取消</button><button class="ui-button danger-solid" :disabled="deleting" @click="removeDetail">{{ deleting ? '删除中…' : '确认删除' }}</button></view>
          </view>
        </view>
      </view>
    </view>

    <view v-if="panel" class="detail-overlay" @click.self="closePanel">
      <view class="detail-panel ui-card" :class="{ 'merge-dialog': panel === 'merge' }" role="dialog" aria-modal="true" :aria-label="panel === 'categories' ? '分类管理' : panel === 'picker' ? '选择食材' : panel === 'merge' ? '合并重复食材' : '食材表单'">
        <IngredientForm v-if="panel === 'create'" :categories="categories" @saved="onSaved" @cancel="panel = null" @open-existing="onOpenExisting" />
        <IngredientForm v-else-if="panel === 'edit' && detail" :key="detail.id" :item="detail" :categories="categories" @saved="onSaved" @cancel="panel = null" @open-existing="onOpenExisting" />
        <CategoryManager v-else-if="panel === 'categories'" :categories="categories" @changed="refreshCategories" @close="panel = null" />
        <IngredientPicker v-else-if="panel === 'picker'" @select="onPickerSelect" @close="panel = null" />
        <IngredientMerge v-else-if="panel === 'merge' && detail" :target="detail" @merged="onMerged" @close="closePanel" />
      </view>
    </view>
  </view>
</template>

<style scoped>
.ingredients-layout { display: flex; flex-direction: column; gap: 24px; }
.page-heading { display: flex; align-items: end; justify-content: space-between; gap: 20px; }
.page-title { display: block; margin: 8px 0 6px; font-size: 30px; font-weight: 700; line-height: 1.2; }
.heading-note { display: block; font-size: 14px; }
.filters { display: flex; gap: 16px; padding: 20px; }
.never-filter { align-self: end; min-height: 44px; margin: 0; padding: 8px 12px; border: 1px solid var(--line); border-radius: 8px; background: white; color: var(--ink); white-space: nowrap; }
.never-filter::after { border: 0; }
.never-filter[aria-pressed='true'] { background: #edf3eb; border-color: var(--green); }
.filter-help { margin-top: -18px; font-size: 13px; color: var(--muted); }
.filter-field { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
.category-field { flex: 0 0 200px; position: relative; }
.filter-label { font-size: 14px; font-weight: 600; }
.picker-value { display: flex; justify-content: space-between; align-items: center; margin: 0; font-weight: 400; text-align: left; cursor: pointer; }
.picker-value::after, .category-option::after { border: 0; }
.category-options { position: absolute; z-index: 10; top: 76px; left: 0; right: 0; max-height: 320px; overflow-y: auto; padding: 4px; box-shadow: 0 8px 22px #173b2e26; }
.category-option { display: block; width: 100%; min-height: 44px; margin: 0; padding: 10px 12px; border: 0; border-radius: 6px; background: #fff; color: var(--ink); text-align: left; cursor: pointer; }
.category-option[aria-current="true"], .category-option:hover { background: #edf3eb; }
.minor-status { margin-top: -16px; font-size: 14px; }
.local-actions { display: flex; flex-wrap: wrap; gap: 12px; margin-top: -18px; }
.selection-note { margin-top: -14px; padding: 10px 12px; border: 1px solid #c9dccb; border-radius: 8px; background: #edf3eb; font-size: 14px; }
.list-card { overflow: hidden; }
.list-head, .list-row { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr) minmax(0, .7fr); align-items: center; gap: 16px; padding: 16px 20px; }
.list-head { background: #f3f6f0; color: var(--muted); font-size: 13px; font-weight: 700; }
.list-row { width: 100%; min-height: 90px; margin: 0; border: 0; border-top: 1px solid var(--line); border-radius: 0; background: #fff; color: var(--ink); text-align: left; cursor: pointer; }
.list-row::after { border: 0; }
.list-row:hover { background: #f6f8f4; }
.list-row:focus-visible, .text-button:focus-visible { outline: 3px solid var(--focus); outline-offset: -3px; }
.name-cell { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.item-title { font-size: 16px; font-weight: 650; }
.item-secondary, .cell-text { font-size: 14px; line-height: 1.45; }
.item-secondary { color: var(--muted); }
.mobile-label { display: none; }
.state-panel { min-height: 124px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; padding: 24px; text-align: center; }
.error-state { color: var(--error); }
.pagination { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; font-size: 14px; color: var(--muted); }
.text-button { display: inline-flex; min-height: 44px; align-items: center; padding: 8px; margin: 0; border: 0; background: transparent; color: var(--green); cursor: pointer; }
.text-button::after { border: 0; }
.detail-overlay { position: fixed; z-index: 30; inset: 0; display: flex; align-items: center; justify-content: center; padding: 24px; background: #0d2c22a6; }
.detail-panel { width: min(100%, 480px); max-height: 90vh; overflow: auto; padding: 24px; }
.merge-dialog { width: min(100%, 680px); }
.detail-top { display: flex; justify-content: space-between; align-items: center; }
.detail-name { display: block; font-size: 24px; font-weight: 700; }
.detail-fields { display: flex; flex-direction: column; gap: 16px; }
.detail-fields > view { display: flex; flex-direction: column; gap: 4px; padding-bottom: 12px; border-bottom: 1px solid var(--line); }
.detail-label { color: var(--muted); font-size: 13px; }
.detail-actions { display: flex; flex-wrap: wrap; gap: 10px; }
.detail-fields > .detail-actions { flex-direction: row; }
.delete-check { display: flex; flex-direction: column; gap: 10px; padding: 12px; border-radius: 8px; background: #fff4e6; font-size: 14px; }
.danger { color: var(--error); }
.danger-solid { background: var(--error); border-color: var(--error); }
@media (max-width: 899px) {
  .ingredients-layout { gap: 18px; }
  .page-title { font-size: 26px; }
  .page-heading .ui-button { flex: none; max-width: 105px; padding: 8px; font-size: 13px; }
  .filters { flex-direction: column; padding: 16px; }
  .category-field { flex: 1; }
  .list-head { display: none; }
  .list-row { display: flex; align-items: start; flex-direction: column; gap: 8px; padding: 18px; }
  .mobile-label { display: inline; color: var(--muted); }
  .detail-overlay { align-items: end; padding: 0; }
  .detail-panel { max-height: 85vh; width: 100%; border-radius: 16px 16px 0 0; padding-bottom: calc(24px + env(safe-area-inset-bottom)); }
}
</style>
