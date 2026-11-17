<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { errorMessage, getFoodItems, mergeFoodItems, previewFoodItemMerge } from '../../shared/api'
import type { FoodItem, MergePlan } from '../../shared/api'

const props = defineProps<{ target: FoodItem }>()
const emit = defineEmits<{ close: []; merged: [plan: MergePlan] }>()
const MAX_SOURCES = 10
const search = ref('')
const results = ref<FoodItem[]>([])
const total = ref(0)
const offset = ref(0)
const searching = ref(false)
const searchError = ref('')
const selected = ref<FoodItem[]>([])
const selectError = ref('')
const preview = ref<MergePlan | null>(null)
const previewError = ref('')
const previewing = ref(false)
const merging = ref(false)
const mergeError = ref('')
const completed = ref<MergePlan | null>(null)
let searchGeneration = 0
let previewGeneration = 0
let disposed = false

const hasMore = computed(() => offset.value < total.value)
const selectedIds = computed(() => selected.value.map(item => item.id))

function noteText(value: string) {
  return value.replace(/\*\*/g, '')
}

function blockerLocation(blocker: MergePlan['blockers'][number]) {
  const d = blocker.details
  if (blocker.code === 'pack_spec_conflict') {
    return `食材 #${d.food_item_id} · ${d.source_name || '不限来源'} · ${d.purchase_unit}；保留项箱规 #${d.target_pack_spec_id}，被并项箱规 #${d.source_pack_spec_id}`
  }
  if (blocker.code === 'incompatible_base_unit') {
    return `食材 #${d.food_item_id} · ${d.from_unit} → ${d.to_unit}`
  }
  return d.food_item_id ? `食材 #${d.food_item_id}` : ''
}

function invalidatePreview() {
  ++previewGeneration
  preview.value = null
  previewError.value = ''
  mergeError.value = ''
}

function toggle(item: FoodItem) {
  if (item.id === props.target.id || merging.value || completed.value) return
  const existing = selected.value.findIndex(source => source.id === item.id)
  if (existing >= 0) selected.value.splice(existing, 1)
  else if (selected.value.length >= MAX_SOURCES) {
    selectError.value = `一次最多合并 ${MAX_SOURCES} 条。合并不可逆，请分几次做。`
    return
  } else selected.value.push(item)
  selectError.value = ''
  invalidatePreview()
}

async function load(reset: boolean) {
  const generation = ++searchGeneration
  const currentOffset = reset ? 0 : offset.value
  searching.value = true
  searchError.value = ''
  if (reset) {
    results.value = []
    offset.value = 0
    total.value = 0
  }
  const params = new URLSearchParams({ limit: '20', offset: String(currentOffset) })
  if (search.value.trim()) params.set('q', search.value.trim())
  try {
    const page = await getFoodItems(`?${params}`)
    if (disposed || generation !== searchGeneration) return
    results.value = reset ? page.items : [...results.value, ...page.items]
    offset.value = page.offset + page.items.length
    total.value = page.total
  } catch (error) {
    if (!disposed && generation === searchGeneration) searchError.value = errorMessage(error)
  } finally {
    if (!disposed && generation === searchGeneration) searching.value = false
  }
}

async function requestPreview() {
  if (!selected.value.length || previewing.value || merging.value) return
  const generation = ++previewGeneration
  previewing.value = true
  preview.value = null
  previewError.value = ''
  try {
    const plan = await previewFoodItemMerge(props.target.id, [...selectedIds.value])
    if (!disposed && generation === previewGeneration) preview.value = plan
  } catch (error) {
    if (!disposed && generation === previewGeneration) previewError.value = errorMessage(error)
  } finally {
    if (!disposed && generation === previewGeneration) previewing.value = false
  }
}

async function executeMerge() {
  if (!preview.value?.can_merge || merging.value) return
  merging.value = true
  mergeError.value = ''
  try {
    const plan = await mergeFoodItems(props.target.id, [...selectedIds.value])
    completed.value = plan
    emit('merged', plan)
  } catch (error) {
    // Another edit may have changed the server plan since the preview.
    invalidatePreview()
    mergeError.value = errorMessage(error)
  } finally {
    merging.value = false
  }
}

onUnmounted(() => {
  disposed = true
  ++searchGeneration
  ++previewGeneration
})
void load(true)
</script>

<template>
  <view class="merge-panel">
    <view class="merge-head"><text class="merge-title">合并重复食材</text><button class="text-button" @click="emit('close')">关闭</button></view>
    <text>把下面选中的食材并入 <strong>{{ target.name }}（#{{ target.id }}）</strong>，保留它的名称与基础单位 {{ target.base_unit }}。</text>
    <template v-if="!completed">
      <view class="search-row">
        <input v-model="search" class="ui-input" placeholder="按名称搜索要并掉的食材" aria-label="搜索被并掉的食材" @confirm="load(true)" />
        <button class="ui-button secondary" :disabled="searching" @click="load(true)">搜索</button>
      </view>
      <text v-if="searchError" class="error-state" role="alert">{{ searchError }}</text>
      <view v-if="results.length" class="results" aria-label="可选择的食材">
        <button v-for="item in results" :key="item.id" class="source-choice" :disabled="item.id === target.id" :aria-pressed="selectedIds.includes(item.id)" @click="toggle(item)">
          <text>{{ selectedIds.includes(item.id) ? '☑' : '□' }} {{ item.name }}（#{{ item.id }}） · {{ item.base_unit }}</text>
          <text v-if="item.id === target.id">保留项</text>
        </button>
      </view>
      <text v-if="searching" role="status">正在读取食材…</text>
      <button v-if="hasMore" class="text-button" :disabled="searching" @click="load(false)">加载更多</button>
      <text v-if="selectError" class="error-state" role="alert">{{ selectError }}</text>
      <view v-if="selected.length" class="selected-list">
        <text>将并入 {{ target.name }}：{{ selected.length }} / {{ MAX_SOURCES }} 条</text>
        <button v-for="item in selected" :key="item.id" class="selected-chip" @click="toggle(item)">{{ item.name }}（#{{ item.id }}） ×</button>
      </view>
      <button class="ui-button secondary" :disabled="!selected.length || previewing || merging" @click="requestPreview">{{ previewing ? '预览中…' : '预览合并影响' }}</button>
      <text v-if="previewError" class="error-state" role="alert">{{ previewError }}</text>
      <view v-if="preview" class="plan" role="status">
        <text class="plan-title">{{ preview.can_merge ? '合并预览' : '合并被拦住' }}</text>
        <text>将搬动：采购记录 {{ preview.moved_purchase_count }} 条、发票行 {{ preview.moved_invoice_line_count }} 条、箱规 {{ preview.moved_pack_spec_count }} 条。</text>
        <text v-for="source in preview.sources" :key="source.food_item.id">{{ source.food_item.name }}（#{{ source.food_item.id }}）：采购 {{ source.purchase_count }}、发票行 {{ source.invoice_line_count }}、箱规 {{ source.pack_spec_count }}<template v-if="source.duplicate_pack_spec_count">（其中完全相同 {{ source.duplicate_pack_spec_count }} 条不搬）</template></text>
        <view v-for="(blocker, index) in preview.blockers" :key="index" class="blocker" role="alert">
          <text>{{ blocker.message }}</text><text v-if="blockerLocation(blocker)">位置：{{ blockerLocation(blocker) }}</text>
        </view>
        <text v-for="(note, index) in preview.notes.filter(note => !note.includes('不可逆'))" :key="index">{{ noteText(note) }}</text>
        <view class="confirm-area">
          <text class="irreversible">{{ noteText(preview.notes.find(note => note.includes('不可逆')) || '合并不可逆。') }}</text>
          <button class="ui-button" :disabled="!preview.can_merge || merging" @click="executeMerge">{{ merging ? '合并中…' : '确认合并' }}</button>
        </view>
      </view>
      <text v-if="mergeError" class="error-state" role="alert">{{ mergeError }} 请重新预览。</text>
    </template>
    <view v-else class="success" role="status">
      <text class="plan-title">已并入 {{ completed.target.name }}</text>
      <text>实际搬动：采购记录 {{ completed.moved_purchase_count }} 条、发票行 {{ completed.moved_invoice_line_count }} 条、箱规 {{ completed.moved_pack_spec_count }} 条。</text>
      <button class="ui-button" @click="emit('close')">完成</button>
    </view>
  </view>
</template>

<style scoped>
.merge-panel, .plan, .success, .selected-list { display: flex; flex-direction: column; gap: 12px; }
.merge-head, .search-row { display: flex; align-items: center; gap: 12px; }
.merge-head { justify-content: space-between; }
.merge-title, .plan-title { font-size: 21px; font-weight: 700; }
.search-row .ui-input { min-width: 0; flex: 1; }
.results { max-height: 240px; overflow-y: auto; border: 1px solid var(--line); border-radius: 8px; }
.source-choice { display: flex; justify-content: space-between; width: 100%; min-height: 44px; padding: 10px; margin: 0; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; background: white; text-align: left; color: var(--ink); }
.source-choice::after, .selected-chip::after { border: 0; }
.source-choice[aria-pressed='true'] { background: #edf3eb; }
.selected-chip { align-self: flex-start; min-height: 44px; margin: 0; padding: 8px 12px; border: 1px solid var(--line); border-radius: 8px; background: #edf3eb; color: var(--ink); }
.plan { padding: 16px; border: 1px solid var(--line); border-radius: 8px; }
.blocker { display: flex; flex-direction: column; gap: 4px; padding: 10px; border: 1px solid var(--error); border-radius: 8px; color: var(--error); }
.confirm-area { display: flex; flex-direction: column; align-items: flex-start; gap: 12px; padding-top: 12px; border-top: 1px solid var(--line); }
.irreversible { color: var(--error); font-weight: 600; }
@media (max-width: 600px) { .search-row { flex-wrap: wrap; } .search-row .ui-input { flex-basis: 100%; } }
</style>
