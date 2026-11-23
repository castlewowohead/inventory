<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ApiError, errorMessage, getMonthlyPrices, getWeeklyPrices, getSourcePrices } from '../../shared/api'
import type { MonthlyPrices, WeeklyPrices, PricePeriod, SourcePrices } from '../../shared/api'
import { exclusionMessages } from './priceCopy'

const props = defineProps<{ foodItemId: number; currency: string | null; mode: 'weekly' | 'monthly' | 'sources'; revision: number }>()
const emit = defineEmits<{ currencies: [codes: string[]]; periodChange: [period: 'weekly' | 'monthly'] }>()
const monthly = ref<MonthlyPrices | null>(null)
const weekly = ref<WeeklyPrices | null>(null)
const sources = ref<SourcePrices | null>(null)
const loading = ref(false)
const errorText = ref('')
const monthCount = ref(6)
const weekCount = ref(6)
const sourcePeriod = ref<'week' | 'month'>('week')
let sequence = 0
const trend = computed(() => props.mode === 'weekly' ? weekly.value : monthly.value)
const trendRows = computed(() => props.mode === 'weekly' ? weekly.value?.weeks ?? [] : monthly.value?.months ?? [])

function money(value: string | null, code: string): string {
  return value === null ? '—' : `${code === 'USD' ? '$' : code === 'CNY' ? '¥' : `${code} `}${value}`
}
function periodText(period: PricePeriod): string {
  return `${period.date_from} 至 ${period.date_to}（${period.days} 天）`
}
function comparisonText(report: MonthlyPrices | WeeklyPrices): string {
  const comparison = report.comparison
  if (comparison.change_percent !== null) return `变化 ${comparison.change_percent}%（${money(comparison.change_amount, report.currency)}/${report.base_unit}）`
  if (comparison.status === 'no_base_data') return '上个比较时段没有采购，暂无涨幅。'
  if (comparison.status === 'no_current_data') return '这个比较时段没有采购，暂无涨幅。'
  if (comparison.status === 'base_price_is_zero') return '上个比较时段的均价为 0，暂无涨幅。'
  return '暂无可比较的采购记录。'
}

function selectTrendPeriod(event: Event) {
  const period = (event.target as HTMLSelectElement).value
  if (period === 'weekly' || period === 'monthly') emit('periodChange', period)
}

// Plot coordinates only. All prices and comparisons displayed below come from the API.
const chart = computed(() => {
  const rows = trendRows.value
  const values = rows.map(row => row.weighted_average_unit_price === null ? null : Number(row.weighted_average_unit_price))
  const present = values.filter((value): value is number => value !== null && Number.isFinite(value))
  if (!present.length) return { segments: [] as string[], dots: [] as Array<{ x: number; y: number }>, min: null as number | null, max: null as number | null }
  const min = Math.min(...present)
  const max = Math.max(...present)
  const x = (index: number) => rows.length === 1 ? 50 : 8 + index * 84 / (rows.length - 1)
  const y = (value: number) => max === min ? 45 : 80 - (value - min) * 65 / (max - min)
  const dots: Array<{ x: number; y: number }> = []
  const segments: string[] = []
  let points: string[] = []
  values.forEach((value, index) => {
    if (value === null || !Number.isFinite(value)) {
      if (points.length > 1) segments.push(points.join(' '))
      points = []
    } else {
      const point = { x: x(index), y: y(value) }
      dots.push(point)
      points.push(`${point.x},${point.y}`)
    }
  })
  if (points.length > 1) segments.push(points.join(' '))
  return { segments, dots, min, max }
})

async function load() {
  const current = ++sequence
  loading.value = true
  errorText.value = ''
  monthly.value = null
  weekly.value = null
  sources.value = null
  try {
    if (props.mode === 'weekly') {
      const result = await getWeeklyPrices(props.foodItemId, props.currency, weekCount.value)
      if (current !== sequence) return
      weekly.value = result
      emit('currencies', result.available_currencies)
    } else if (props.mode === 'monthly') {
      const result = await getMonthlyPrices(props.foodItemId, props.currency, monthCount.value)
      if (current !== sequence) return
      monthly.value = result
      emit('currencies', result.available_currencies)
    } else {
      const result = await getSourcePrices(props.foodItemId, props.currency, sourcePeriod.value)
      if (current !== sequence) return
      sources.value = result
      emit('currencies', result.available_currencies)
    }
  } catch (error) {
    if (current !== sequence) return
    if (error instanceof ApiError && error.status === 400 && Array.isArray(error.details?.available)) {
      emit('currencies', error.details.available.filter((value): value is string => typeof value === 'string'))
      errorText.value = '请选择一种币种查看价格。'
    } else errorText.value = errorMessage(error)
  } finally { if (current === sequence) loading.value = false }
}
watch(() => [props.foodItemId, props.currency, props.mode, props.revision, monthCount.value, weekCount.value, sourcePeriod.value], () => { void load() }, { immediate: true })
</script>

<template>
  <view class="analytics">
    <view v-if="mode === 'weekly' || mode === 'monthly'" class="panel ui-card">
      <view class="heading"><view class="period-heading"><text class="section-title">{{ mode === 'weekly' ? '周均价' : '月均价' }}</text><select class="ui-input period-select" :value="mode" aria-label="均价周期" @change="selectTrendPeriod"><option value="weekly">按周</option><option value="monthly">按月</option></select></view><label v-if="mode === 'weekly'">查看周数 <select v-model.number="weekCount" class="ui-input"><option :value="6">6 周</option><option :value="12">12 周</option></select></label><label v-else>查看月数 <select v-model.number="monthCount" class="ui-input"><option :value="6">6 个月</option><option :value="12">12 个月</option></select></label></view>
      <text class="muted">按购买金额和数量计算平均单价。</text>
      <text v-if="loading" role="status">正在加载价格…</text>
      <text v-if="errorText" class="error" role="alert">{{ errorText }} <button class="link-button" @click="load">重试</button></text>
      <template v-if="trend && !loading">
        <view class="comparison">
          <text>{{ mode === 'weekly' ? '本周' : '本月' }} {{ periodText(trend.comparison.current.period) }}：{{ money(trend.comparison.current.weighted_average_unit_price, trend.currency) }}/{{ trend.base_unit }}</text>
          <text>{{ mode === 'weekly' ? '上周同期' : '上月同期' }} {{ periodText(trend.comparison.base.period) }}：{{ money(trend.comparison.base.weighted_average_unit_price, trend.currency) }}/{{ trend.base_unit }}</text>
          <text class="section-title">{{ comparisonText(trend) }}</text>
          <text v-if="trend.comparison.current.period.basis === 'month_to_date' || trend.comparison.current.period.basis === 'week_to_date'" class="muted">{{ mode === 'weekly' ? '本周与上周' : '本月与上月' }}按相同天数比较。</text>
        </view>
        <view class="chart" role="img" :aria-label="`${mode === 'weekly' ? '周' : '月'}均价折线，单位 ${trend.currency}/${trend.base_unit}；无数据时段断开`">
          <text class="axis-title">{{ trend.currency }}/{{ trend.base_unit }}</text>
          <view class="plot">
            <view class="axis-labels"><text>{{ chart.max === null ? '—' : chart.max }}</text><text>{{ chart.min === null ? '—' : chart.min }}</text></view>
            <svg class="line-chart" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
              <line x1="0" y1="80" x2="100" y2="80" stroke="#bbc8c0" stroke-width="0.6" />
              <polyline v-for="(points, index) in chart.segments" :key="index" :points="points" fill="none" stroke="#176b4d" stroke-width="1.5" vector-effect="non-scaling-stroke" />
              <circle v-for="(point, index) in chart.dots" :key="index" :cx="point.x" :cy="point.y" r="1.4" fill="#176b4d" />
            </svg>
          </view>
          <view class="month-grid"><view v-for="row in trendRows" :key="row.period.key" class="month-cell"><text>{{ mode === 'weekly' ? `${row.period.date_from.slice(5)} 至 ${row.period.date_to.slice(5)}` : row.period.key }}</text><text>{{ money(row.weighted_average_unit_price, trend.currency) }}</text><text class="muted">{{ row.purchase_count }} 笔采购</text></view></view>
        </view>
        <view v-if="exclusionMessages(trend.excluded).length" class="footnotes"><text v-for="message in exclusionMessages(trend.excluded)" :key="message">{{ message }}</text></view>
      </template>
    </view>
    <view v-else class="panel ui-card">
      <view class="heading"><text class="section-title">采购来源比价</text><label>查看范围 <select v-model="sourcePeriod" class="ui-input"><option value="week">近 12 周</option><option value="month">近 3 个月</option></select></label></view>
      <text v-if="loading" role="status">正在加载来源价格…</text>
      <text v-if="errorText" class="error" role="alert">{{ errorText }} <button class="link-button" @click="load">重试</button></text>
      <template v-if="sources && !loading">
        <text class="muted">{{ periodText(sources.period) }} · 按每 {{ sources.base_unit }} 比较。价格也可能受规格和品质影响。</text>
        <text v-if="!sources.sources.length">该期间没有可比较的采购来源。</text>
        <text v-else class="section-title">平均价格最低：{{ sources.sources[0].source_name || '来源未填' }}</text>
        <text v-if="sources.sources.length > 1 && sources.spread_amount !== null">最高与最低均价相差 {{ money(sources.spread_amount, sources.currency) }}/{{ sources.base_unit }}<template v-if="sources.spread_percent !== null">（{{ sources.spread_percent }}%）</template>。</text>
        <view v-for="(row, index) in sources.sources" :key="`${row.source_name_normalized}-${index}`" class="source-row">
          <view class="source-head"><text class="section-title">{{ row.source_name || '来源未填' }} <text v-if="index === 0" class="badge">最低均价</text></text><text class="price">{{ money(row.weighted_average_unit_price, sources.currency) }}/{{ sources.base_unit }}</text></view>
          <text class="muted">{{ row.purchase_count }} 笔采购 · {{ row.first_purchase_date }} 至 {{ row.latest_purchase_date }} · 共 {{ row.total_base_quantity }} {{ sources.base_unit }}</text>
          <text v-for="(description, i) in row.descriptions" :key="i">{{ description }}</text>
        </view>
        <view v-if="exclusionMessages(sources.excluded).length" class="footnotes"><text v-for="message in exclusionMessages(sources.excluded)" :key="message">{{ message }}</text></view>
      </template>
    </view>
  </view>
</template>

<style scoped>
.analytics, .panel, .comparison, .footnotes, .source-row, .chart { display:flex; flex-direction:column; gap:12px; min-width:0; }
.panel { padding:18px; overflow-wrap:anywhere; }
.heading, .source-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
.heading { flex-wrap:wrap; align-items:center; }
.period-heading { display:flex; flex:none; align-items:center; gap:12px; }
.period-heading .section-title { flex:none; white-space:nowrap; }
.period-select { min-width:120px; }
.heading label { display:flex; align-items:center; gap:8px; margin-left:auto; white-space:nowrap; }
.section-title { font-size:16px; font-weight:700; }
.muted { color:var(--muted); font-size:13px; }
.error { color:var(--error); }
.link-button { border:0; background:none; color:var(--green); cursor:pointer; }
.comparison { padding:12px; background:#f2f7f2; border-radius:6px; }
.chart { padding-top:8px; }
.axis-title { font-size:13px; font-weight:600; }
.plot { display:flex; gap:8px; height:160px; min-width:0; }
.axis-labels { display:flex; flex-direction:column; justify-content:space-between; font-size:12px; color:var(--muted); }
.line-chart { display:block; flex:1; width:100%; min-width:0; height:100%; }
.month-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(90px,1fr)); gap:6px; }
.month-cell { display:flex; flex-direction:column; gap:3px; padding:6px; background:#f7f9f6; font-size:12px; min-width:0; }
.source-row { border-top:1px solid var(--line); padding-top:12px; }
.price { font-size:18px; font-weight:700; white-space:nowrap; }
.badge { font-size:12px; color:#176b4d; }
.footnotes { border-top:1px solid var(--line); padding-top:10px; font-size:13px; color:var(--muted); }
@media (max-width:650px) { .panel { padding:14px; } .source-head { flex-direction:column; } .month-grid { grid-template-columns:repeat(3,minmax(0,1fr)); } .plot { height:130px; } }
</style>
