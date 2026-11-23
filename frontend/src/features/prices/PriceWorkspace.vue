<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import IngredientPicker from '../ingredients/IngredientPicker.vue'
import PriceAnalytics from './PriceAnalytics.vue'
import PriceAlertsPanel from './PriceAlertsPanel.vue'
import { exclusionMessages } from './priceCopy'
import { ApiError, errorMessage, getFoodItem, getPriceHistory } from '../../shared/api'
import type { FoodItem, PriceHistory } from '../../shared/api'

const props = defineProps<{ foodItemId: number | null }>()
const emit = defineEmits<{ select: [id: number | null]; back: [] }>()
const item = ref<FoodItem | null>(null)
const pickerOpen = ref(false)
const history = ref<PriceHistory | null>(null)
const currency = ref<string | null>(null)
const availableCurrencies = ref<string[]>([])
const busy = ref(false)
const errorText = ref('')
const activeTab = ref<'average' | 'history' | 'sources' | 'alerts'>('average')
const averagePeriod = ref<'weekly' | 'monthly'>('weekly')
const revision = ref(0)
let requestId = 0

function showPrice(value: string | null): string {
  // The API already returns a decimal string rounded to up to four places.
  // Preserve it, including 0 and very small values such as 0.0052.
  return value === null ? '—' : value
}

function currencySymbol(code: string | null): string {
  return code === 'USD' ? '$' : code === 'CNY' ? '¥' : code ? `${code} ` : ''
}

async function loadSelected(offset = 0) {
  const id = props.foodItemId
  if (!id) { item.value = null; history.value = null; availableCurrencies.value = []; return }
  const current = ++requestId
  busy.value = true
  errorText.value = ''
  if (item.value?.id !== id) { item.value = null; history.value = null }
  try {
    const food = await getFoodItem(id)
    if (current !== requestId) return
    item.value = food
    const page = await getPriceHistory(id, currency.value, offset)
    if (current !== requestId) return
    history.value = page
    availableCurrencies.value = page.available_currencies
    // No automatic choice when several currencies exist. A single currency is
    // merely reported by the server; it is not sent as a filter.
  } catch (error) {
    if (current !== requestId) return
    if (error instanceof ApiError && error.status === 400 && Array.isArray(error.details?.available)) {
      availableCurrencies.value = error.details.available.filter((value): value is string => typeof value === 'string')
      errorText.value = '请选择一种币种查看价格。'
    } else {
      errorText.value = errorMessage(error)
    }
    history.value = null
  } finally {
    if (current === requestId) busy.value = false
  }
}

function updateCurrencies(codes: string[]) {
  availableCurrencies.value = codes
}

function selectFromAlert(id: number) {
  selectItem(id)
  activeTab.value = 'average'
  averagePeriod.value = 'weekly'
}

function selectItem(id: number) {
  pickerOpen.value = false
  currency.value = null
  emit('select', id)
}

function selectCurrency(value: string) {
  currency.value = value || null
  void loadSelected(0)
}

watch(() => props.foodItemId, () => { currency.value = null; availableCurrencies.value = []; void loadSelected(0) })
onMounted(() => { void loadSelected() })
</script>

<template>
  <view class="price-workspace">
    <button class="back-ingredients" @click="emit('back')">← 返回食材</button>
    <view class="heading">
      <view><text class="eyebrow">价格历史</text><text class="title">{{ item?.name_zh || item?.name || '选择一条食材' }}</text><text v-if="item?.name_zh" class="muted">{{ item.name }}</text></view>
      <button v-if="foodItemId !== null" class="ui-button secondary" @click="pickerOpen = true">切换食材</button>
    </view>
    <view v-if="pickerOpen" class="ui-card picker"><IngredientPicker mode="suggest" @select="selectItem" @close="pickerOpen = false" /></view>
    <text v-if="errorText && activeTab === 'history'" class="error" role="alert">{{ errorText }}</text>

    <view class="price-tabs" role="tablist" aria-label="价格视图">
      <button v-for="tab in ([['average', '均价'], ['history', '采购历史'], ['sources', '来源比价'], ['alerts', '涨价提醒']] as const)" :key="tab[0]" class="price-tab" :class="{ selected: activeTab === tab[0] }" role="tab" :aria-selected="activeTab === tab[0]" @click="activeTab = tab[0]">{{ tab[1] }}</button>
    </view>
    <PriceAlertsPanel v-if="activeTab === 'alerts'" @select="selectFromAlert" />

    <template v-if="item && activeTab !== 'alerts'">
      <view class="toolbar ui-card">
        <view><text class="section-title">采购价格</text><text class="muted">按每 {{ item.base_unit }} 比较</text></view>
        <view v-if="availableCurrencies.length > 1" class="currency-control">
          <label for="price-currency">币种</label>
          <select id="price-currency" class="ui-input" :value="currency || ''" @change="selectCurrency(($event.target as HTMLSelectElement).value)">
            <option value="">全部币种（逐条查看）</option>
            <option v-for="code in availableCurrencies" :key="code" :value="code">{{ code }}</option>
          </select>
          <text class="muted">查看均价或来源比价时，请选择一种币种。</text>
        </view>
      </view>
      <PriceAnalytics v-if="activeTab === 'average' || activeTab === 'sources'" :food-item-id="item.id" :currency="currency" :mode="activeTab === 'average' ? averagePeriod : 'sources'" :revision="revision" @currencies="updateCurrencies" @period-change="averagePeriod = $event" />
      <text v-if="busy && activeTab === 'history'" role="status">正在加载价格历史…</text>
      <template v-if="history && !busy && activeTab === 'history'">
        <view class="summary ui-card">
          <text>共 {{ history.total }} 笔采购</text>
          <view v-if="exclusionMessages(history.excluded).length" class="notices"><text v-for="message in exclusionMessages(history.excluded)" :key="message">{{ message }}</text></view>
        </view>
        <view v-if="history.items.length === 0" class="empty ui-card">暂无采购价格记录。</view>
        <view v-for="row in history.items" :key="row.purchase.id" class="history-row ui-card">
          <view class="row-head"><text class="section-title">{{ row.purchase.purchase_date || '日期未填' }}<template v-if="row.purchase.source_name"> · {{ row.purchase.source_name }}</template></text></view>
          <text v-if="row.purchase.original_description" class="description">{{ row.purchase.original_description }}</text>
          <text>购买 {{ row.purchase.quantity ?? '—' }} {{ row.purchase.purchase_unit || '' }} · 共 {{ currencySymbol(row.purchase.currency) }}{{ row.purchase.line_amount ?? '—' }}</text>
          <view class="price-line">
            <text v-if="row.conversion.status === 'available'" class="price-value">每 {{ row.conversion.base_unit }} {{ currencySymbol(row.purchase.currency) }}{{ showPrice(row.conversion.normalized_unit_price) }}</text>
            <text v-else class="price-value muted">暂不能比较单价</text>
            <text v-if="row.conversion.status === 'pending_pack_spec'" class="warning">采购单位需要统一。</text>
            <text v-else-if="row.conversion.status === 'unit_unknown'" class="warning">采购单位无法识别，请检查采购记录。</text>
            <text v-else-if="row.conversion.status === 'incompatible_unit'" class="warning">采购单位与食材单位不一致，请检查采购记录。</text>
          </view>
        </view>
        <view v-if="history.total > history.limit" class="pager"><button class="ui-button secondary" :disabled="history.offset === 0" @click="loadSelected(Math.max(0, history.offset - history.limit))">上一页</button><text>{{ history.offset + 1 }}–{{ Math.min(history.offset + history.items.length, history.total) }} / {{ history.total }}</text><button class="ui-button secondary" :disabled="history.offset + history.limit >= history.total" @click="loadSelected(history.offset + history.limit)">下一页</button></view>
      </template>

    </template>
    <button v-else-if="foodItemId === null && !pickerOpen && activeTab !== 'alerts'" class="ui-button select-ingredient" @click="pickerOpen = true">选择食材</button>
  </view>
</template>

<style scoped>
.price-workspace { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.back-ingredients { align-self: flex-start; width: auto; min-height: 40px; margin: 0; padding: 4px 0; border: 0; background: transparent; color: var(--green); font-size: 15px; cursor: pointer; }
.back-ingredients::after { border: 0; }
.price-tabs { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:6px; min-width:0; }
.price-tab { min-width:0; margin:0; padding:10px 4px; font-size:14px; white-space:normal; line-height:1.3; background:#fff; border:1px solid var(--line); color:var(--green); }
.price-tab.selected { background:var(--green); color:#fff; }
.price-tab::after { border:0; }
.heading, .row-head, .toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
.heading > view:first-child, .toolbar > view:first-child { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.title { display: block; font-size: 28px; font-weight: 700; line-height: 1.3; }
.section-title { display: block; font-size: 16px; font-weight: 700; }
.picker, .toolbar, .summary, .history-row, .empty { padding: 18px; min-width: 0; }
.history-row, .summary { display: flex; flex-direction: column; gap: 12px; }
.history-row { overflow-wrap: anywhere; }
.description { font-weight: 600; }
.price-line, .notices { display: flex; flex-direction: column; gap: 5px; }
.price-value { font-size: 18px; font-weight: 700; }
.warning { color: #704007; line-height: 1.45; }
.muted { color: var(--muted); font-size: 13px; line-height: 1.4; overflow-wrap: anywhere; }
.error { color: var(--error); line-height: 1.5; }
.heading > button { flex: none; }
.select-ingredient { width: 100%; min-height: 76px; font-size: 20px; }
.pager { display: flex; align-items: center; gap: 8px; }
.pager { justify-content: center; flex-wrap: wrap; }
.currency-control { display: flex; flex-direction: column; gap: 4px; min-width: 180px; }
@media (max-width: 650px) {
  .heading, .toolbar { flex-direction: column; }
  .heading > button { width: 100%; }
  .title { font-size: 23px; }
  .picker, .toolbar, .summary, .history-row, .empty { padding: 14px; }
}
</style>
