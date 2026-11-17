<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ApiError, errorMessage, getMonthlyPrices, getSourcePrices } from '../../shared/api'
import type { MonthlyPrices, PriceExclusions, PricePeriod, SourcePrices } from '../../shared/api'

const props = defineProps<{ foodItemId: number; currency: string | null; mode: 'monthly' | 'sources'; revision: number }>()
const emit = defineEmits<{ currencies: [codes: string[]] }>()
const monthly = ref<MonthlyPrices | null>(null)
const sources = ref<SourcePrices | null>(null)
const loading = ref(false)
const errorText = ref('')
const monthCount = ref(6)
let sequence = 0

function money(value: string | null, code: string): string {
  return value === null ? '—' : `${code === 'USD' ? '$' : code === 'CNY' ? '¥' : `${code} `}${value}`
}
function periodText(period: PricePeriod): string {
  return `${period.date_from} 至 ${period.date_to}（${period.days} 天）`
}
function exclusionText(excluded: PriceExclusions): string {
  return `缺日期 ${excluded.undated} · 单位未知 ${excluded.unit_unknown} · 待补箱规 ${excluded.pending_pack_spec} · 单位不兼容 ${excluded.incompatible_unit} · 其他币种 ${excluded.other_currency}`
}
function comparisonText(report: MonthlyPrices): string {
  const comparison = report.comparison
  if (comparison.change_percent !== null) return `变化 ${comparison.change_percent}%（${money(comparison.change_amount, report.currency)}/${report.base_unit}）`
  if (comparison.status === 'no_base_data') return '基期没有采购，无法计算涨幅。'
  if (comparison.status === 'no_current_data') return '本期没有采购，无法计算涨幅。'
  if (comparison.status === 'base_price_is_zero') return '基期均价为 0，涨幅无定义。'
  return comparison.reason || '没有可比数据。'
}

// Plot coordinates only. All prices and comparisons displayed below come from the API.
const chart = computed(() => {
  const rows = monthly.value?.months ?? []
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
  sources.value = null
  try {
    if (props.mode === 'monthly') {
      const result = await getMonthlyPrices(props.foodItemId, props.currency, monthCount.value)
      if (current !== sequence) return
      monthly.value = result
      emit('currencies', result.available_currencies)
    } else {
      const result = await getSourcePrices(props.foodItemId, props.currency)
      if (current !== sequence) return
      sources.value = result
      emit('currencies', result.available_currencies)
    }
  } catch (error) {
    if (current !== sequence) return
    if (error instanceof ApiError && error.status === 400 && Array.isArray(error.details?.available)) {
      emit('currencies', error.details.available.filter((value): value is string => typeof value === 'string'))
      errorText.value = `${error.message} 请选择币种后重查。`
    } else errorText.value = errorMessage(error)
  } finally { if (current === sequence) loading.value = false }
}
watch(() => [props.foodItemId, props.currency, props.mode, props.revision, monthCount.value], () => { void load() }, { immediate: true })
</script>

<template>
  <view class="analytics">
    <view v-if="mode === 'monthly'" class="panel ui-card">
      <view class="heading"><text class="section-title">月度加权均价</text><label>查看月数 <select v-model.number="monthCount" class="ui-input"><option :value="6">6 个月</option><option :value="12">12 个月</option></select></label></view>
      <text class="muted">按期间总金额 ÷ 基础单位总数量；只统计可换算的采购。</text>
      <text v-if="loading" role="status">正在加载月度价格…</text>
      <text v-if="errorText" class="error" role="alert">{{ errorText }} <button class="link-button" @click="load">重试</button></text>
      <template v-if="monthly && !loading">
        <view class="comparison">
          <text>本期 {{ periodText(monthly.comparison.current.period) }}：{{ money(monthly.comparison.current.weighted_average_unit_price, monthly.currency) }}/{{ monthly.base_unit }}</text>
          <text>基期 {{ periodText(monthly.comparison.base.period) }}：{{ money(monthly.comparison.base.weighted_average_unit_price, monthly.currency) }}/{{ monthly.base_unit }}</text>
          <text class="section-title">{{ comparisonText(monthly) }}</text>
          <text v-if="monthly.comparison.current.period.basis === 'month_to_date'" class="muted">截至今天 {{ monthly.comparison.current.period.days }} 天 vs 上月同样 {{ monthly.comparison.base.period.days }} 天。</text>
        </view>
        <view class="chart" role="img" :aria-label="`月度加权均价折线，单位 ${monthly.currency}/${monthly.base_unit}；无数据月份断开`">
          <text class="axis-title">{{ monthly.currency }}/{{ monthly.base_unit }}</text>
          <view class="plot">
            <view class="axis-labels"><text>{{ chart.max === null ? '—' : chart.max }}</text><text>{{ chart.min === null ? '—' : chart.min }}</text></view>
            <svg class="line-chart" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
              <line x1="0" y1="80" x2="100" y2="80" stroke="#bbc8c0" stroke-width="0.6" />
              <polyline v-for="(points, index) in chart.segments" :key="index" :points="points" fill="none" stroke="#176b4d" stroke-width="1.5" vector-effect="non-scaling-stroke" />
              <circle v-for="(point, index) in chart.dots" :key="index" :cx="point.x" :cy="point.y" r="1.4" fill="#176b4d" />
            </svg>
          </view>
          <view class="month-grid"><view v-for="row in monthly.months" :key="row.period.key" class="month-cell"><text>{{ row.period.key }}</text><text>{{ money(row.weighted_average_unit_price, monthly.currency) }}</text><text class="muted">{{ row.purchase_count }} 条</text><text class="muted">记录 {{ row.purchase_ids.join('、') || '无' }}</text></view></view>
        </view>
        <view class="footnotes"><text>未纳入统计 {{ monthly.excluded.total }} 条：{{ exclusionText(monthly.excluded) }}</text><text v-for="(note, index) in monthly.notes" :key="index">{{ note }}</text></view>
      </template>
    </view>
    <view v-else class="panel ui-card">
      <text class="section-title">采购来源比价</text>
      <text v-if="loading" role="status">正在加载来源价格…</text>
      <text v-if="errorText" class="error" role="alert">{{ errorText }} <button class="link-button" @click="load">重试</button></text>
      <template v-if="sources && !loading">
        <text class="muted">期间 {{ periodText(sources.period) }}；均按 {{ sources.base_unit }} 折算。请结合原始描述与箱规判断规格、品质是否可比。</text>
        <text v-if="!sources.sources.length">该期间没有可比较的采购来源。</text>
        <text v-else class="section-title">按基础单位折算后最便宜的是 {{ sources.sources[0].source_name || '来源未填' }}。</text>
        <text v-if="sources.sources.length > 1 && sources.spread_amount !== null">最高与最低均价相差 {{ money(sources.spread_amount, sources.currency) }}/{{ sources.base_unit }}<template v-if="sources.spread_percent !== null">（{{ sources.spread_percent }}%）</template>。</text>
        <view v-for="(row, index) in sources.sources" :key="`${row.source_name_normalized}-${index}`" class="source-row">
          <view class="source-head"><text class="section-title">{{ row.source_name || '来源未填' }} <text v-if="index === 0" class="badge">最低均价</text></text><text class="price">{{ money(row.weighted_average_unit_price, sources.currency) }}/{{ sources.base_unit }}</text></view>
          <text class="muted">{{ row.purchase_count }} 条 · {{ row.first_purchase_date }} 至 {{ row.latest_purchase_date }} · {{ row.total_base_quantity }} {{ sources.base_unit }}</text>
          <text v-for="(description, i) in row.descriptions" :key="i">原始描述：{{ description }}</text>
          <text>箱规：{{ row.pack_spec ? `1 ${row.pack_spec.purchase_unit} = ${row.pack_spec.pack_size} ${row.pack_spec.pack_unit}（${row.pack_spec.source_name || '不限来源'}）` : '无箱规' }}</text>
          <text v-if="row.pack_spec?.spec_text" class="muted">规格原文：{{ row.pack_spec.spec_text }}</text>
        </view>
        <view class="footnotes"><text>未纳入统计 {{ sources.excluded.total }} 条：{{ exclusionText(sources.excluded) }}</text><text v-for="(note, index) in sources.notes" :key="index">{{ note }}</text></view>
      </template>
    </view>
  </view>
</template>

<style scoped>
.analytics, .panel, .comparison, .footnotes, .source-row, .chart { display:flex; flex-direction:column; gap:12px; min-width:0; }
.panel { padding:18px; overflow-wrap:anywhere; }
.heading, .source-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
.heading label { display:flex; align-items:center; gap:8px; }
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
