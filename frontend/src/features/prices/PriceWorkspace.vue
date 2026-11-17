<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import IngredientPicker from '../ingredients/IngredientPicker.vue'
import PriceAnalytics from './PriceAnalytics.vue'
import PriceAlertsPanel from './PriceAlertsPanel.vue'
import { ApiError, deletePackSpec, errorMessage, getFoodItem, getPackSpecs, getPendingPackSpecs, getPriceHistory, getPriceUnits, savePackSpec } from '../../shared/api'
import type { FoodItem, PackSpec, PendingPackSpec, PriceHistory, PriceUnits, Purchase } from '../../shared/api'

const props = defineProps<{ foodItemId: number | null }>()
const emit = defineEmits<{ select: [id: number | null] }>()
const item = ref<FoodItem | null>(null)
const pickerOpen = ref(false)
const history = ref<PriceHistory | null>(null)
const pending = ref<PendingPackSpec[]>([])
const pendingTotal = ref(0)
const specs = ref<PackSpec[]>([])
const catalog = ref<PriceUnits | null>(null)
const currency = ref<string | null>(null)
const availableCurrencies = ref<string[]>([])
const busy = ref(false)
const saving = ref(false)
const errorText = ref('')
const pendingError = ref('')
const resultText = ref('')
const formOpen = ref(false)
const selectedPending = ref<PendingPackSpec | null>(null)
const selectedSpec = ref<PackSpec | null>(null)
const form = ref({ source_name: '', purchase_unit: '', pack_size: '', pack_unit: '', spec_text: '', note: '' })
const acceptedSuggestion = ref(false)
const activeTab = ref<'history' | 'monthly' | 'sources' | 'alerts'>('history')
const revision = ref(0)
let requestId = 0

const ownPending = computed(() => pending.value.filter(row => row.food_item.id === props.foodItemId))
const measurementGroups = computed(() => catalog.value?.dimensions ?? [])
const packagingUnits = computed(() => catalog.value?.packaging_units ?? [])
const selectedDimension = computed(() => measurementGroups.value.find(group => group.units.some(unit => unit.unit === item.value?.base_unit)))
const compatibleUnits = computed(() => selectedDimension.value?.units.map(unit => unit.unit) ?? [])
const preSubmitCount = computed(() => {
  const source = form.value.source_name.normalize('NFKC').trim().replace(/\s+/g, ' ').toLocaleLowerCase()
  const pendingCount = ownPending.value
    .filter(row => row.purchase_unit === form.value.purchase_unit && (source ? row.source_name_normalized === source || row === selectedPending.value && row.source_name === form.value.source_name : true))
    .reduce((total, row) => total + row.purchase_count, 0)
  const spec = selectedSpec.value
  const visibleAlreadyConverted = spec && spec.purchase_unit === form.value.purchase_unit && (spec.source_name ?? '') === form.value.source_name
    ? history.value?.items.filter(row => row.conversion.pack_spec?.id === spec.id).length ?? 0
    : 0
  return pendingCount + visibleAlreadyConverted
})

function showPrice(value: string | null): string {
  // The API already returns a decimal string rounded to up to four places.
  // Preserve it, including 0 and very small values such as 0.0052.
  return value === null ? '—' : value
}

function currencySymbol(code: string | null): string {
  return code === 'USD' ? '$' : code === 'CNY' ? '¥' : code ? `${code} ` : ''
}

function purchaseLine(purchase: Purchase) {
  return `${purchase.quantity ?? '—'} ${purchase.purchase_unit ?? '—'} · ${currencySymbol(purchase.currency)}${purchase.line_amount ?? '—'}`
}

async function loadCommon() {
  const [units, groups] = await Promise.allSettled([getPriceUnits(), getPendingPackSpecs()])
  if (units.status === 'fulfilled') catalog.value = units.value
  if (groups.status === 'fulfilled') {
    pending.value = groups.value.items
    pendingTotal.value = groups.value.total
  }
  pendingError.value = [units, groups].filter(result => result.status === 'rejected')
    .map(result => errorMessage((result as PromiseRejectedResult).reason)).join('；')
}

async function loadSelected(offset = 0) {
  const id = props.foodItemId
  if (!id) { item.value = null; history.value = null; specs.value = []; availableCurrencies.value = []; return }
  const current = ++requestId
  busy.value = true
  errorText.value = ''
  if (item.value?.id !== id) { item.value = null; history.value = null; specs.value = [] }
  try {
    const food = await getFoodItem(id)
    if (current !== requestId) return
    item.value = food
    const [page, packPage] = await Promise.all([
      getPriceHistory(id, currency.value, offset), getPackSpecs(id)
    ])
    if (current !== requestId) return
    const allSpecs = [...packPage.items]
    while (allSpecs.length < packPage.total) {
      const next = await getPackSpecs(id, allSpecs.length)
      if (current !== requestId || next.items.length === 0) return
      allSpecs.push(...next.items)
    }
    history.value = page
    availableCurrencies.value = page.available_currencies
    specs.value = allSpecs
    // No automatic choice when several currencies exist. A single currency is
    // merely reported by the server; it is not sent as a filter.
  } catch (error) {
    if (current !== requestId) return
    if (error instanceof ApiError && error.status === 400 && Array.isArray(error.details?.available)) {
      availableCurrencies.value = error.details.available.filter((value): value is string => typeof value === 'string')
      errorText.value = `${error.message} 请选择币种后重查。`
    } else {
      errorText.value = errorMessage(error)
    }
    history.value = null
  } finally {
    if (current === requestId) busy.value = false
  }
}

async function refresh() {
  await Promise.all([loadCommon(), loadSelected(history.value?.offset ?? 0)])
  revision.value += 1
}

function updateCurrencies(codes: string[]) {
  availableCurrencies.value = codes
}

function selectFromAlert(id: number) {
  selectItem(id)
  activeTab.value = 'monthly'
}

function selectItem(id: number) {
  pickerOpen.value = false
  currency.value = null
  resultText.value = ''
  emit('select', id)
}

function selectCurrency(value: string) {
  currency.value = value || null
  void loadSelected(0)
}

function onSuggestionConfirm(event: { detail: { value: string[] } }) {
  acceptedSuggestion.value = event.detail.value.includes('confirmed')
}

function openForm(row: PendingPackSpec | null = null, spec: PackSpec | null = null) {
  selectedPending.value = row
  selectedSpec.value = spec
  acceptedSuggestion.value = false
  form.value = {
    source_name: row?.source_name ?? spec?.source_name ?? '',
    purchase_unit: row?.purchase_unit ?? spec?.purchase_unit ?? '',
    pack_size: row?.suggestion?.pack_size ?? spec?.pack_size ?? '',
    pack_unit: row?.suggestion?.pack_unit ?? spec?.pack_unit ?? '',
    spec_text: row?.suggestion?.spec_text ?? spec?.spec_text ?? '',
    note: spec?.note ?? ''
  }
  errorText.value = ''
  formOpen.value = true
}

async function submit() {
  if (!item.value || saving.value) return
  errorText.value = ''
  if (!packagingUnits.value.includes(form.value.purchase_unit)) {
    errorText.value = '请选择目录中的包装采购单位。'
    return
  }
  if (!compatibleUnits.value.includes(form.value.pack_unit)) {
    errorText.value = '箱内单位须与食材基础单位同量纲。'
    return
  }
  if (!/^\d+(?:\.\d{1,4})?$/.test(form.value.pack_size) || !/[1-9]/.test(form.value.pack_size)) {
    errorText.value = '请填写大于 0 的箱内数量，最多 4 位小数。'
    return
  }
  if (selectedPending.value?.suggestion?.confirmation_required && !acceptedSuggestion.value) {
    errorText.value = '请先对照原始描述确认候选箱规。'
    return
  }
  saving.value = true
  try {
    const saved = await savePackSpec({
      food_item_id: item.value.id,
      source_name: form.value.source_name.trim() || null,
      purchase_unit: form.value.purchase_unit,
      pack_size: form.value.pack_size,
      pack_unit: form.value.pack_unit,
      spec_text: form.value.spec_text.trim() || null,
      note: form.value.note.trim() || null
    })
    resultText.value = `${saved.created ? '箱规已新增。' : '箱规已更新。'}影响 ${saved.affected_purchase_count} 条采购记录（含历史）。${saved.notice}`
    formOpen.value = false
    await refresh()
  } catch (error) {
    errorText.value = errorMessage(error)
  } finally {
    saving.value = false
  }
}

async function removeSpec(spec: PackSpec) {
  const answer = await new Promise<boolean>(resolve => uni.showModal({
    title: '删除箱规？',
    content: '删除后，受影响的历史价格将重新变成「待补箱规」。采购记录本身不会改变。',
    success: result => resolve(result.confirm), fail: () => resolve(false)
  }))
  if (!answer) return
  errorText.value = ''
  try {
    const result = await deletePackSpec(spec.id)
    resultText.value = result.message
    await refresh()
  } catch (error) { errorText.value = errorMessage(error) }
}

watch(() => props.foodItemId, () => { currency.value = null; availableCurrencies.value = []; formOpen.value = false; void loadSelected(0) })
onMounted(() => { void loadCommon(); void loadSelected() })
</script>

<template>
  <view class="price-workspace">
    <view class="heading">
      <view><text class="eyebrow">价格历史</text><text class="title">{{ item?.name_zh || item?.name || '选择一条食材' }}</text><text v-if="item?.name_zh" class="muted">{{ item.name }}</text></view>
      <button class="ui-button secondary" @click="pickerOpen = true">{{ item ? '更换食材' : '选择食材' }}</button>
    </view>
    <view v-if="pickerOpen" class="ui-card picker"><IngredientPicker mode="suggest" @select="selectItem" @close="pickerOpen = false" /></view>
    <text v-if="resultText" class="notice" role="status">{{ resultText }}</text>
    <text v-if="errorText && activeTab === 'history'" class="error" role="alert">{{ errorText }}</text>
    <text v-if="pendingError && activeTab === 'history'" class="error" role="alert">待补箱规或单位目录读取失败：{{ pendingError }} <button class="link-button" @click="loadCommon">重试</button></text>

    <view class="price-tabs" role="tablist" aria-label="价格视图">
      <button v-for="tab in ([['history', '采购历史'], ['monthly', '月度均价'], ['sources', '来源比价'], ['alerts', '涨价提醒']] as const)" :key="tab[0]" class="price-tab" :class="{ selected: activeTab === tab[0] }" role="tab" :aria-selected="activeTab === tab[0]" @click="activeTab = tab[0]">{{ tab[1] }}</button>
    </view>
    <PriceAlertsPanel v-if="activeTab === 'alerts'" @select="selectFromAlert" />

    <template v-if="item && activeTab !== 'alerts'">
      <view class="toolbar ui-card">
        <view><text class="section-title">{{ item.name }} 的采购价格</text><text class="muted">基础单位 {{ item.base_unit }} · 标准化价格按当前箱规实时计算</text></view>
        <view v-if="availableCurrencies.length > 1" class="currency-control">
          <label for="price-currency">币种</label>
          <select id="price-currency" class="ui-input" :value="currency || ''" @change="selectCurrency(($event.target as HTMLSelectElement).value)">
            <option value="">全部币种（逐条查看）</option>
            <option v-for="code in availableCurrencies" :key="code" :value="code">{{ code }}</option>
          </select>
          <text class="muted">不同币种不混算；月度与来源统计须选定一种币种。</text>
        </view>
      </view>
      <PriceAnalytics v-if="activeTab === 'monthly' || activeTab === 'sources'" :food-item-id="item.id" :currency="currency" :mode="activeTab" :revision="revision" @currencies="updateCurrencies" />
      <text v-if="busy && activeTab === 'history'" role="status">正在加载价格历史…</text>
      <template v-if="history && !busy && activeTab === 'history'">
        <view class="summary ui-card">
          <text>共 {{ history.total }} 条采购记录 · 可换算 {{ history.convertible_count }} 条</text>
          <view class="excluded"><text class="section-title">未纳入统计：{{ history.excluded.total }} 条</text><text>缺日期 {{ history.excluded.undated }} · 单位未知 {{ history.excluded.unit_unknown }} · 待补箱规 {{ history.excluded.pending_pack_spec }} · 单位不兼容 {{ history.excluded.incompatible_unit }} · 其他币种 {{ history.excluded.other_currency }}</text></view>
          <view class="notes"><text v-for="(note, index) in history.notes" :key="index">{{ note }}</text><text v-if="!history.notes.some(note => note.includes('不含运费'))">商品行价格不含运费与整单折让。</text></view>
        </view>
        <view v-if="history.items.length === 0" class="empty ui-card">暂无采购价格记录。</view>
        <view v-for="row in history.items" :key="row.purchase.id" class="history-row ui-card">
          <view class="row-head"><text class="section-title">{{ row.purchase.purchase_date || '日期待补' }} · {{ row.purchase.source_name || '来源未填' }}</text><text class="muted">采购记录 #{{ row.purchase.id }}</text></view>
          <text v-if="row.purchase.original_description" class="description">{{ row.purchase.original_description }}</text>
          <text>原始：{{ purchaseLine(row.purchase) }} · 单价 {{ currencySymbol(row.purchase.currency) }}{{ row.purchase.unit_price ?? '—' }}/{{ row.purchase.purchase_unit ?? '—' }}</text>
          <view class="price-line">
            <text v-if="row.conversion.status === 'available'" class="price-value">标准化：{{ currencySymbol(row.purchase.currency) }}{{ showPrice(row.conversion.normalized_unit_price) }}/{{ row.conversion.base_unit }}</text>
            <text v-else class="price-value muted">标准化：—/{{ row.conversion.base_unit }}</text>
            <text v-if="row.conversion.status === 'pending_pack_spec'" class="warning">待补箱规：按包装单位计价，尚无法换算。</text>
            <text v-else-if="row.conversion.status === 'unit_unknown'" class="warning">这个采购单位认不出来；请在采购记录中修正单位。</text>
            <text v-else-if="row.conversion.status === 'incompatible_unit'" class="warning">体积与重量没有密度不能换算；请核对基础单位或实际称重。</text>
          </view>
          <text v-if="row.conversion.pack_spec" class="muted">箱规：1 {{ row.conversion.pack_spec.purchase_unit }} = {{ row.conversion.pack_spec.pack_size }} {{ row.conversion.pack_spec.pack_unit }} · {{ row.conversion.pack_spec_match === 'exact_source' ? '精确来源' : '不限来源' }}</text>
          <text v-if="row.conversion.reason" class="muted">{{ row.conversion.reason }}</text>
          <button v-if="row.conversion.status === 'pending_pack_spec'" class="ui-button secondary" @click="openForm(ownPending.find(group => group.purchase_unit === row.purchase.purchase_unit_normalized && group.source_name_normalized === (row.purchase.source_name?.toLocaleLowerCase() ?? null)) ?? null)">补箱规</button>
        </view>
        <view v-if="history.total > history.limit" class="pager"><button class="ui-button secondary" :disabled="history.offset === 0" @click="loadSelected(Math.max(0, history.offset - history.limit))">上一页</button><text>{{ history.offset + 1 }}–{{ Math.min(history.offset + history.items.length, history.total) }} / {{ history.total }}</text><button class="ui-button secondary" :disabled="history.offset + history.limit >= history.total" @click="loadSelected(history.offset + history.limit)">下一页</button></view>
      </template>

      <view v-if="activeTab === 'history'" class="spec-section ui-card">
        <view class="row-head"><view><text class="section-title">待补箱规</text><text class="muted">服务端按涉及金额排序；当前食材 {{ ownPending.length }} 组</text></view><button class="ui-button secondary" @click="openForm()">手工填写箱规</button></view>
        <text v-if="pendingTotal > pending.length" class="warning">待补清单共 {{ pendingTotal }} 组，当前只显示前 {{ pending.length }} 组。</text>
        <text v-if="!ownPending.length" class="muted">当前食材没有待补组。</text>
        <view v-for="(row, index) in ownPending" :key="index" class="pending-row">
          <view><text>{{ row.source_name || '来源未填' }} · {{ row.purchase_unit }} · {{ row.purchase_count }} 条采购</text><text class="muted">涉及金额 {{ row.total_amount }} · 最近 {{ row.latest_purchase_date || '日期待补' }}</text><text v-for="(description, i) in row.descriptions" :key="i" class="muted">原始描述：{{ description }}</text><text v-if="row.suggestion" class="warning">候选：1 {{ row.purchase_unit }} = {{ row.suggestion.pack_size }} {{ row.suggestion.pack_unit }}。须对照原件确认。</text><text v-else class="muted">无可靠候选，请人工填写；例如 6X30 不自动解释。</text></view>
          <button class="ui-button secondary" @click="openForm(row)">补箱规</button>
        </view>
      </view>

      <view v-if="activeTab === 'history'" class="spec-section ui-card">
        <text class="section-title">已保存箱规</text>
        <text v-if="!specs.length" class="muted">暂无。</text>
        <view v-for="spec in specs" :key="spec.id" class="saved-spec"><view><text>1 {{ spec.purchase_unit }} = {{ spec.pack_size }} {{ spec.pack_unit }} · {{ spec.source_name || '不限来源' }}</text><text v-if="spec.spec_text" class="muted">{{ spec.spec_text }}</text></view><view class="actions"><button class="ui-button secondary" @click="openForm(null, spec)">修改</button><button class="ui-button secondary" @click="removeSpec(spec)">删除</button></view></view>
      </view>

      <view v-if="formOpen && activeTab === 'history'" class="spec-section ui-card" aria-label="补录箱规">
        <text class="section-title">确认箱规</text>
        <text class="warning">箱规保存后历史标准化价格会立即重算。提交前：当前待补清单中至少 {{ preSubmitCount }} 条采购记录可能受影响；提交后显示后端计算的实际条数。</text>
        <text v-if="selectedPending?.suggestion" class="muted">原始描述：{{ selectedPending.suggestion.from_description }}。候选仅用于预填，请核对数量与单位。</text>
        <view class="form-grid">
          <label>采购包装单位<select v-model="form.purchase_unit" class="ui-input"><option value="">选择包装单位</option><option v-for="unit in packagingUnits" :key="unit" :value="unit">{{ unit }}</option></select></label>
          <label>箱内数量<input v-model="form.pack_size" class="ui-input" type="text" inputmode="decimal" placeholder="例如 40" /></label>
          <label>箱内单位<select v-model="form.pack_unit" class="ui-input"><option value="">选择单位</option><option v-for="unit in compatibleUnits" :key="unit" :value="unit">{{ unit }}</option></select></label>
          <label>来源<input v-model="form.source_name" class="ui-input" placeholder="留空 = 不限来源" /></label>
          <label>规格原文<input v-model="form.spec_text" class="ui-input" placeholder="可选" /></label>
          <label>备注<input v-model="form.note" class="ui-input" placeholder="可选" /></label>
        </view>
        <text class="muted">来源留空 = 不限来源；有精确来源箱规时，精确来源优先。</text>
        <checkbox-group v-if="selectedPending?.suggestion?.confirmation_required" @change="onSuggestionConfirm">
          <label class="confirm"><checkbox value="confirmed" :checked="acceptedSuggestion" /> 我已对照原始描述，确认箱内数量与单位</label>
        </checkbox-group>
        <view class="actions"><button class="ui-button secondary" @click="formOpen = false">取消</button><button class="ui-button" :disabled="saving || !catalog" @click="submit">{{ saving ? '保存中…' : '确认并保存箱规' }}</button></view>
      </view>
    </template>
    <view v-else-if="!pickerOpen && activeTab !== 'alerts'" class="empty ui-card">选择食材后查看每条采购的原始价格、换算状态与待补箱规。</view>
  </view>
</template>

<style scoped>
.price-workspace { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.price-tabs { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:6px; min-width:0; }
.price-tab { min-width:0; margin:0; padding:10px 4px; font-size:14px; white-space:normal; line-height:1.3; background:#fff; border:1px solid var(--line); color:var(--green); }
.price-tab.selected { background:var(--green); color:#fff; }
.price-tab::after { border:0; }
.heading, .row-head, .toolbar, .saved-spec, .pending-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
.heading > view:first-child, .toolbar > view:first-child, .pending-row > view, .saved-spec > view:first-child { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.title { display: block; font-size: 28px; font-weight: 700; line-height: 1.3; }
.section-title { display: block; font-size: 16px; font-weight: 700; }
.picker, .toolbar, .summary, .history-row, .spec-section, .empty { padding: 18px; min-width: 0; }
.history-row, .spec-section, .summary { display: flex; flex-direction: column; gap: 12px; }
.history-row { overflow-wrap: anywhere; }
.description { font-weight: 600; }
.price-line, .notes, .excluded, .pending-row > view, .saved-spec > view:first-child { display: flex; flex-direction: column; gap: 5px; }
.price-value { font-size: 18px; font-weight: 700; }
.warning { color: #704007; line-height: 1.45; }
.muted { color: var(--muted); font-size: 13px; line-height: 1.4; overflow-wrap: anywhere; }
.notice { display: block; padding: 12px; border-radius: 7px; background: #e8f2e9; line-height: 1.5; }
.error { color: var(--error); line-height: 1.5; }
.pending-row, .saved-spec { padding: 12px 0; border-top: 1px solid var(--line); }
.pending-row > button, .heading > button { flex: none; }
.actions, .pager { display: flex; align-items: center; gap: 8px; }
.pager { justify-content: center; flex-wrap: wrap; }
.currency-control { display: flex; flex-direction: column; gap: 4px; min-width: 180px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.form-grid label { display: flex; flex-direction: column; gap: 5px; font-size: 14px; }
.confirm { display: flex; align-items: center; gap: 8px; min-height: 44px; }
.link-button { border: 0; background: transparent; color: var(--green); cursor: pointer; }
.link-button::after { border: 0; }
@media (max-width: 650px) {
  .heading, .toolbar, .pending-row, .saved-spec { flex-direction: column; }
  .heading > button, .pending-row > button { width: 100%; }
  .actions { flex-wrap: wrap; }
  .form-grid { grid-template-columns: minmax(0, 1fr); }
  .title { font-size: 23px; }
  .picker, .toolbar, .summary, .history-row, .spec-section, .empty { padding: 14px; }
}
</style>
