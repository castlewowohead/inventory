<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { errorMessage, getFoodItems, getFoodItemSuggestions } from '../../shared/api'
import type { CategoryRef, FoodItem, FoodItemSuggestion, FoodItemSuggestions } from '../../shared/api'

const props = withDefaults(defineProps<{ initialQuery?: string; mode?: 'library' | 'suggest' }>(), { initialQuery: '', mode: 'library' })
const emit = defineEmits<{ select: [id: number]; close: [] }>()
const query = ref(props.initialQuery)
const appliedQuery = ref(props.initialQuery)
const items = ref<FoodItem[]>([])
const suggestions = ref<FoodItemSuggestions | null>(null)
const total = ref(0)
const nextOffset = ref(0)
const state = ref<'loading' | 'ready' | 'error'>('loading')
const errorText = ref('')
const loadingMore = ref(false)
let generation = 0
let disposed = false
let timer: ReturnType<typeof setTimeout> | null = null

function categoryDisplay(category: CategoryRef) {
  return category.name_zh ? `${category.name_zh} / ${category.name}` : category.name
}

function path(offset: number) {
  const params = new URLSearchParams()
  if (appliedQuery.value.trim()) params.set('q', appliedQuery.value.trim())
  params.set('limit', '20')
  params.set('offset', String(offset))
  return `?${params.toString()}`
}

async function reload() {
  const current = ++generation
  state.value = 'loading'
  errorText.value = ''
  items.value = []
  suggestions.value = null
  total.value = 0
  nextOffset.value = 0
  try {
    if (props.mode === 'suggest') {
      const result = await getFoodItemSuggestions(appliedQuery.value)
      if (disposed || current !== generation) return
      suggestions.value = result
      state.value = 'ready'
      return
    }
    const page = await getFoodItems(path(0))
    if (disposed || current !== generation) return
    items.value = page.items
    total.value = page.total
    nextOffset.value = page.offset + page.items.length
    state.value = 'ready'
  } catch (error) {
    if (disposed || current !== generation) return
    errorText.value = errorMessage(error)
    state.value = 'error'
  }
}

function reasonLabel(item: FoodItemSuggestion): string {
  switch (item.match_reason) {
    case 'exact': return '精确匹配'
    case 'prefix': return '开头匹配'
    case 'contains': return '名称包含'
    case 'fuzzy': return '模糊匹配'
    case 'merged_from': return '旧名称已合并'
    case 'recent': return '最近采购'
    case 'frequent': return '常用采购'
  }
}

function scoreLabel(score: number | null): string {
  return score === null ? '无文本分数' : `分数 ${score.toFixed(4)}`
}

async function loadMore() {
  if (state.value !== 'ready' || loadingMore.value || nextOffset.value >= total.value) return
  const current = generation
  loadingMore.value = true
  errorText.value = ''
  try {
    const page = await getFoodItems(path(nextOffset.value))
    if (disposed || current !== generation) return
    const known = new Set(items.value.map(item => item.id))
    items.value = [...items.value, ...page.items.filter(item => !known.has(item.id))]
    total.value = page.total
    nextOffset.value = page.offset + page.items.length
    if (page.items.length === 0) nextOffset.value = total.value
  } catch (error) {
    if (!disposed && current === generation) errorText.value = errorMessage(error)
  } finally {
    if (!disposed && current === generation) loadingMore.value = false
  }
}

function onInput(event: Event) {
  const inputEvent = event as Event & { detail?: { value: string } }
  query.value = inputEvent.detail?.value ?? (event.target as HTMLInputElement | null)?.value ?? ''
  if (props.mode === 'suggest') {
    // An earlier response must not stay selectable while a new name is typed.
    ++generation
    suggestions.value = null
    state.value = 'loading'
  }
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => {
    appliedQuery.value = query.value
    void reload()
  }, 300)
}

onMounted(() => void reload())
onUnmounted(() => {
  disposed = true
  ++generation
  if (timer) clearTimeout(timer)
})
</script>

<template>
  <view class="picker-body">
    <view class="picker-head"><text class="picker-title">{{ mode === 'suggest' ? '食材建议' : '选择食材' }}</text><button class="text-button" @click="emit('close')">关闭</button></view>
    <text class="picker-note">找到后请点击「选择」。列表不会自动决定食材。</text>
    <input class="ui-input" :value="query" :aria-label="mode === 'suggest' ? '查找食材建议' : '选择器搜索食材'" :placeholder="mode === 'suggest' ? '输入名称查模糊候选；清空看最近与常用' : '搜索英文名、中文名或规格'" @input="onInput" />
    <view v-if="state === 'loading'" class="picker-state" role="status">正在查找食材…</view>
    <view v-else-if="state === 'error'" class="picker-state error" role="alert">{{ errorText }}<button class="ui-button secondary" @click="reload">重试</button></view>
    <template v-else-if="mode === 'suggest' && suggestions">
      <view class="picker-results" role="status" aria-live="polite">
        <view v-for="group in (appliedQuery.trim()
          ? [{ title: '名称匹配', items: suggestions.matches }]
          : [{ title: '最近', items: suggestions.recent }, { title: '常用', items: suggestions.frequent }])"
          :key="group.title" class="suggest-group">
          <text class="suggest-title">{{ group.title }}</text>
          <text v-if="!group.items.length" class="picker-note">{{ appliedQuery.trim() ? '没有匹配的食材，可改用别的名称或新建。' : '暂无记录。' }}</text>
          <view v-for="item in group.items" :key="`${group.title}-${item.food_item.id}`" class="picker-row">
            <view class="picker-info">
              <text class="picker-name">{{ item.food_item.name }}</text>
              <text v-if="item.food_item.name_zh" class="muted">{{ item.food_item.name_zh }}</text>
              <text class="muted">{{ categoryDisplay(item.food_item.category) }} · {{ item.food_item.base_unit }}</text>
              <text class="muted">{{ reasonLabel(item) }} ({{ item.match_reason }}) · {{ scoreLabel(item.score) }}</text>
              <text v-if="item.match_reason === 'merged_from'" class="merged-note">「{{ item.merged_from }}」这个旧叫法已经合并到「{{ item.food_item.name }}」。</text>
              <text class="muted">历史采购 {{ item.purchase_count ?? '—' }} 次 · 最近采购 {{ item.last_purchase_date ?? '—' }}</text>
            </view>
            <button class="ui-button secondary" :aria-label="`选择 ${item.food_item.name}`" @click="emit('select', item.food_item.id)">选择</button>
          </view>
        </view>
      </view>
      <view v-if="suggestions.notes.length" class="suggest-notes">
        <text v-for="(note, index) in suggestions.notes" :key="index">{{ note }}</text>
      </view>
    </template>
    <view v-else-if="items.length === 0" class="picker-state" role="status">没有符合条件的食材。</view>
    <view v-else class="picker-results">
      <view v-for="item in items" :key="item.id" class="picker-row">
        <view class="picker-info"><text class="picker-name">{{ item.name }}</text><text v-if="item.name_zh" class="muted">{{ item.name_zh }}</text><text class="muted">{{ categoryDisplay(item.category) }} · {{ item.base_unit }}</text></view>
        <button class="ui-button secondary" :aria-label="`选择 ${item.name}`" @click="emit('select', item.id)">选择</button>
      </view>
      <button v-if="nextOffset < total" class="ui-button secondary more" :disabled="loadingMore" @click="loadMore">{{ loadingMore ? '正在加载…' : '加载更多' }}</button>
      <text v-if="errorText" class="error" role="alert">{{ errorText }}；可重试加载更多。</text>
    </view>
  </view>
</template>

<style scoped>
.picker-body { display: flex; flex-direction: column; gap: 14px; }
.picker-head { display: flex; align-items: center; justify-content: space-between; }
.picker-title { font-size: 22px; font-weight: 700; }
.picker-note { color: var(--muted); font-size: 13px; line-height: 1.45; }
.picker-state { min-height: 90px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; text-align: center; }
.picker-results { max-height: 50vh; overflow-y: auto; }
.picker-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--line); }
.picker-info { min-width: 0; display: flex; flex-direction: column; gap: 4px; font-size: 14px; }
.picker-name { font-weight: 650; }
.picker-info .muted { font-size: 13px; }
.suggest-group { display: flex; flex-direction: column; gap: 6px; padding: 8px 0 14px; }
.suggest-title { font-size: 15px; font-weight: 700; }
.merged-note { color: var(--warning, #704007); font-size: 13px; line-height: 1.5; }
.suggest-notes { display: flex; flex-direction: column; gap: 5px; padding: 10px 12px; border-radius: 6px; background: #fff5e9; color: #704007; font-size: 13px; line-height: 1.5; }
.picker-row .ui-button { flex: none; padding: 8px 12px; }
.more { width: 100%; margin-top: 12px; }
.error { color: var(--error); font-size: 13px; }
.text-button { min-height: 44px; padding: 8px; margin: 0; border: 0; background: transparent; color: var(--green); cursor: pointer; }
.text-button::after { border: 0; }
</style>
