<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  createCategory, errorMessage, getCategories, getSupplierCandidates, matchDescription,
  type Category, type InvoiceLine, type MatchCandidate
} from '../../shared/api'
import IngredientPicker from '../ingredients/IngredientPicker.vue'

const props = defineProps<{
  invoiceId: number
  line: InvoiceLine
  invoiceSource: string | null
  description: string
  busy: boolean
  docClass: string | null
}>()
const emit = defineEmits<{
  choose: [id: number]
  clear: []
  categoryChange: [id: number | null]
}>()

const categories = ref<Category[]>([])
const categoryId = ref<number | null>(null)
const categoryOpen = ref(false)
const creatingCategory = ref(false)
const newCategoryName = ref('')
const categoryBusy = ref(false)
const categoryError = ref('')
const showPicker = ref(false)
const lookup = ref<'none' | 'supplier' | 'description'>('none')
const lookupBusy = ref(false)
const lookupError = ref('')
const found = ref<MatchCandidate[]>([])
const sourceOverride = ref('')
let lookupSequence = 0

const categoryLabel = computed(() => {
  const category = categories.value.find((item) => item.id === categoryId.value)
  return category ? category.name_zh || category.name : '选择分类'
})
const effectiveSource = computed(() => sourceOverride.value.trim() || props.line.source_name || props.invoiceSource || '')
const suggestedCategory = computed(() => categories.value.find(
  (item) => item.id === props.line.food_item_match?.suggested_category_id))

async function loadCategories() {
  categoryError.value = ''
  try {
    categories.value = (await getCategories()).items
  } catch (error) {
    categoryError.value = errorMessage(error)
  }
}

function chooseCategory(id: number) {
  categoryId.value = id
  categoryOpen.value = false
  creatingCategory.value = false
  emit('categoryChange', id)
}

async function addCategory() {
  const name = newCategoryName.value.trim()
  if (!name) { categoryError.value = '请填写分类名称。'; return }
  if (categoryBusy.value) return
  categoryBusy.value = true
  categoryError.value = ''
  try {
    const category = await createCategory({ name, name_zh: null })
    await loadCategories()
    if (!categories.value.some((item) => item.id === category.id)) categories.value.push(category)
    newCategoryName.value = ''
    chooseCategory(category.id)
  } catch (error) {
    categoryError.value = errorMessage(error)
  } finally { categoryBusy.value = false }
}

function choose(id: number) {
  showPicker.value = false
  emit('choose', id)
}

async function runLookup(kind: 'supplier' | 'description') {
  const sequence = ++lookupSequence
  lookup.value = kind
  lookupBusy.value = true
  lookupError.value = ''
  found.value = []
  try {
    const response = kind === 'supplier'
      ? await getSupplierCandidates(props.invoiceId, sourceOverride.value.trim() || null)
      : await matchDescription(props.description.trim(), effectiveSource.value || null)
    if (sequence === lookupSequence) found.value = response.candidates
  } catch (error) {
    if (sequence === lookupSequence) lookupError.value = errorMessage(error)
  } finally {
    if (sequence === lookupSequence) lookupBusy.value = false
  }
}

function candidateNote(candidate: MatchCandidate) {
  const parts = [candidate.same_source ? '同一来源' : '其它来源', `历史采购 ${candidate.purchase_count} 次`]
  if (candidate.last_purchase_date) parts.push(`最近 ${candidate.last_purchase_date}`)
  return parts.join(' · ')
}

watch(() => props.line.id, () => {
  lookupSequence += 1
  lookup.value = 'none'
  found.value = []
  lookupError.value = ''
  sourceOverride.value = ''
  categoryId.value = null
  categoryOpen.value = false
  creatingCategory.value = false
  showPicker.value = false
  emit('categoryChange', null)
  void loadCategories()
}, { immediate: true })
</script>

<template>
  <view class="matcher">
    <text class="matcher-title">食材 *</text>
    <view v-if="line.food_item" class="chosen" role="status">
      <view>
        <text class="chosen-name">{{ line.food_item.name }}</text>
        <text class="muted small">{{ line.food_item.category.name_zh || line.food_item.category.name }} · {{ line.food_item.base_unit }}</text>
        <text v-if="line.food_item.needs_review" class="small">分类待确认；可在食材页修改。</text>
      </view>
      <button class="ui-button secondary" :disabled="busy" @click="emit('clear')">更换食材</button>
    </view>
    <view v-else-if="line.line_kind === 'purchase'" class="first-item">
      <text class="muted small">首次入账这种商品时，会用本行的商品名称和采购单位创建食材；这里只需选分类。</text>
      <text v-if="suggestedCategory" class="muted small">系统建议分类：{{ suggestedCategory.name_zh || suggestedCategory.name }}，请核对后选择。</text>
      <view class="category-field">
        <text class="field-label">分类 *</text>
        <button class="ui-input select-button" :aria-expanded="categoryOpen" :disabled="busy" @click="categoryOpen = !categoryOpen">
          {{ categoryLabel }} <text aria-hidden="true">⌄</text>
        </button>
        <view v-if="categoryOpen" class="category-menu ui-card">
          <button v-for="category in categories" :key="category.id" class="option" @click="chooseCategory(category.id)">
            {{ category.name_zh || category.name }}
          </button>
          <button class="option add-option" @click="creatingCategory = true">＋ 新建分类</button>
          <view v-if="creatingCategory" class="new-category">
            <input v-model="newCategoryName" class="ui-input" aria-label="新分类名称" placeholder="分类名称" />
            <button class="ui-button" :disabled="categoryBusy" @click="addCategory">{{ categoryBusy ? '创建中…' : '创建并选用' }}</button>
          </view>
        </view>
        <text v-if="categoryError" class="error-text" role="alert">{{ categoryError }}</text>
      </view>
    </view>

    <view v-if="line.match_candidates.length" class="candidates">
      <text class="group-title">历史匹配</text>
      <view v-for="candidate in line.match_candidates" :key="candidate.food_item.id" class="candidate">
        <view><text class="chosen-name">{{ candidate.food_item.name }}</text><text class="muted small">{{ candidateNote(candidate) }}</text></view>
        <button class="ui-button secondary" :disabled="busy" @click="choose(candidate.food_item.id)">选用</button>
      </view>
    </view>
    <view class="lookup-actions">
      <button class="ui-button secondary" :disabled="busy" @click="runLookup('supplier')">按供应商历史查候选</button>
      <button class="ui-button secondary" :disabled="busy || !description.trim()" @click="runLookup('description')">按商品名称查历史匹配</button>
      <button class="ui-button secondary" :disabled="busy" @click="showPicker = true">查找已有食材</button>
    </view>
    <text v-if="docClass === 'handwritten_scan'" class="muted small">手写件请对照原件核对商品名称。</text>
    <view class="source-row">
      <text class="muted small">查候选用的来源：{{ effectiveSource || '未填写' }}</text>
      <input v-model="sourceOverride" class="ui-input" placeholder="临时指定来源（不会保存）" aria-label="临时指定来源" />
    </view>
    <view v-if="lookup !== 'none'" class="lookup-result">
      <text class="group-title">{{ lookup === 'supplier' ? '该来源历史采购' : '商品名称历史匹配' }}</text>
      <text v-if="lookupBusy" role="status">正在查找…</text>
      <text v-else-if="lookupError" class="error-text" role="alert">{{ lookupError }}</text>
      <text v-else-if="!found.length" class="muted small">没有找到已有食材；选择分类后可在确认入账时自动创建。</text>
      <view v-for="candidate in found" :key="candidate.food_item.id" class="candidate">
        <view><text class="chosen-name">{{ candidate.food_item.name }}</text><text class="muted small">{{ candidateNote(candidate) }}</text></view>
        <button class="ui-button secondary" :disabled="busy" @click="choose(candidate.food_item.id)">选用</button>
      </view>
    </view>
    <view v-if="showPicker" class="picker-overlay" @click.self="showPicker = false">
      <view class="picker-panel ui-card" role="dialog" aria-modal="true" aria-label="选择已有食材">
        <IngredientPicker mode="suggest" :initial-query="description.trim()" @select="choose" @close="showPicker = false" />
      </view>
    </view>
  </view>
</template>

<style scoped>
.matcher { display: flex; flex-direction: column; gap: 12px; min-width: 0; padding: 16px; border: 1px solid var(--line); border-radius: 8px; background: #f9faf7; }
.matcher-title,.group-title { font-size: 14px; font-weight: 650; }
.small { display: block; font-size: 12px; line-height: 1.5; }
.chosen,.candidate { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.chosen { padding: 12px; border: 1px solid #c9dccb; border-radius: 7px; background: #edf3eb; }
.chosen-name { display: block; font-size: 14px; font-weight: 650; overflow-wrap: anywhere; }
.chosen .ui-button,.candidate .ui-button { flex: none; }
.first-item,.category-field,.candidates,.lookup-result,.source-row { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.category-field { position: relative; }
.field-label { font-size: 13px; font-weight: 650; }
.select-button { display: flex; align-items: center; justify-content: space-between; margin: 0; text-align: left; font-weight: 400; cursor: pointer; }
.select-button::after,.option::after { border: 0; }
.category-menu { max-height: 260px; overflow-y: auto; padding: 4px; box-shadow: 0 8px 22px #173b2e26; }
.option { display: block; width: 100%; min-height: 44px; margin: 0; padding: 9px 12px; border: 0; border-radius: 6px; background: #fff; color: var(--ink); text-align: left; cursor: pointer; }
.add-option { color: var(--green); font-weight: 650; border-top: 1px solid var(--line); }
.new-category { display: flex; flex-wrap: wrap; gap: 8px; padding: 8px; }
.new-category .ui-input { flex: 1 1 170px; }
.candidate { padding: 10px 0; border-bottom: 1px solid var(--line); }
.lookup-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.lookup-actions .ui-button { flex: 1 1 auto; padding: 10px 12px; font-size: 14px; }
.lookup-result { padding-top: 10px; border-top: 1px solid var(--line); }
.error-text { color: var(--error); font-size: 13px; }
.picker-overlay { position: fixed; z-index: 30; inset: 0; display: flex; align-items: center; justify-content: center; padding: 24px; background: #0d2c22a6; }
.picker-panel { width: min(100%, 480px); max-height: 90vh; overflow: auto; padding: 24px; }
@media (max-width: 899px) { .picker-overlay { align-items: end; padding: 0; }.picker-panel { width: 100%; max-height: 85vh; border-radius: 16px 16px 0 0; } }
</style>
