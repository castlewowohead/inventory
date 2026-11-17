<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { errorMessage, getAlertSettings, getPriceAlerts, putAlertSettings } from '../../shared/api'
import type { AlertSettings, PriceAlerts, PricePeriod } from '../../shared/api'

const emit = defineEmits<{ select: [id: number] }>()
const settings = ref<AlertSettings | null>(null)
const report = ref<PriceAlerts | null>(null)
const enabled = ref(true)
const savedThreshold = ref('10')
const temporaryThreshold = ref('')
const loading = ref(false)
const saving = ref(false)
const errorText = ref('')
const resultText = ref('')
let sequence = 0

const skippedRows = computed(() => Object.entries(report.value?.skipped ?? {}).filter(([, count]) => count > 0))
const statusLabels: Record<string, string> = {
  no_base_data: '基期没有采购',
  no_current_data: '本期没有采购',
  base_price_is_zero: '基期均价为 0'
}
function periodText(period: PricePeriod): string {
  return `${period.date_from} 至 ${period.date_to}（${period.days} 天）`
}
function validThreshold(value: string): boolean {
  return /^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/.test(value) && Number(value) <= 1000
}
function onEnabledChange(event: { detail: { value: string[] } }) {
  enabled.value = event.detail.value.includes('enabled')
}
async function load() {
  const current = ++sequence
  loading.value = true
  errorText.value = ''
  try {
    // Both endpoints lazily create the singleton settings row in a fresh DB.
    // Read settings first so the two requests cannot race to insert it.
    const nextSettings = await getAlertSettings()
    const nextReport = await getPriceAlerts()
    if (current !== sequence) return
    settings.value = nextSettings
    enabled.value = nextSettings.enabled
    savedThreshold.value = nextSettings.threshold_percent
    report.value = nextReport
  } catch (error) { if (current === sequence) errorText.value = errorMessage(error) }
  finally { if (current === sequence) loading.value = false }
}
async function save() {
  if (saving.value) return
  errorText.value = ''
  resultText.value = ''
  if (!validThreshold(savedThreshold.value)) { errorText.value = '阈值请填 0 至 1000，最多两位小数。'; return }
  saving.value = true
  try {
    const next = await putAlertSettings({ enabled: enabled.value, threshold_percent: savedThreshold.value })
    settings.value = next
    savedThreshold.value = next.threshold_percent
    resultText.value = '提醒设置已保存。'
    report.value = await getPriceAlerts()
  } catch (error) { errorText.value = errorMessage(error) }
  finally { saving.value = false }
}
async function preview() {
  errorText.value = ''
  resultText.value = ''
  if (!validThreshold(temporaryThreshold.value)) { errorText.value = '临时阈值请填 0 至 1000，最多两位小数。'; return }
  loading.value = true
  try {
    report.value = await getPriceAlerts(temporaryThreshold.value)
    resultText.value = `本次仅按 ${report.value.threshold_percent}% 查询，保存的设置未改变。`
  } catch (error) { errorText.value = errorMessage(error) }
  finally { loading.value = false }
}
watch(() => settings.value, value => { if (value) enabled.value = value.enabled })
void load()
</script>

<template>
  <view class="alerts-panel ui-card">
    <text class="section-title">涨价提醒</text>
    <text class="muted">按食材与币种分别比较；只展示达到阈值的上涨，无法比较的食材单列。</text>
    <text v-if="loading" role="status">正在加载提醒…</text>
    <text v-if="errorText" class="error" role="alert">{{ errorText }} <button class="link-button" @click="load">重试</button></text>
    <text v-if="resultText" class="notice" role="status">{{ resultText }}</text>
    <template v-if="settings">
      <view class="controls">
        <checkbox-group class="toggle" @change="onEnabledChange"><label><checkbox value="enabled" :checked="enabled" /> 启用提醒</label></checkbox-group>
        <label>保存的阈值（%）<input v-model="savedThreshold" class="ui-input" inputmode="decimal" type="text" /></label>
        <button class="ui-button" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存设置' }}</button>
      </view>
      <view class="controls preview"><label>临时查看阈值（%）<input v-model="temporaryThreshold" class="ui-input" inputmode="decimal" type="text" placeholder="例如 20" /></label><button class="ui-button secondary" :disabled="loading" @click="preview">仅本次查询</button><button class="ui-button secondary" :disabled="loading" @click="load">恢复保存值</button></view>
    </template>
    <template v-if="report && !loading">
      <text class="muted">当前采用 {{ report.threshold_percent }}% 阈值 · {{ report.settings.enabled ? '提醒已启用' : '提醒已关闭' }}</text>
      <text class="muted">本期 {{ periodText(report.current_period) }}；基期 {{ periodText(report.base_period) }}。截至本月今天时，基期取上月同样天数。</text>
      <view class="alert-results">
        <view><text class="section-title">达到阈值 · {{ report.alerts.length }} 组</text><text class="muted">已比较 {{ report.compared_count }} 组（每组是一种食材和一种币种）。</text></view>
        <text v-if="report.alerts.length === 0">没有达到本次阈值的上涨。</text>
        <button v-for="row in report.alerts" :key="`${row.food_item.id}-${row.currency}`" class="alert-row" @click="emit('select', row.food_item.id)">
          <text class="section-title">{{ row.food_item.name_zh || row.food_item.name }} · {{ row.currency }} · 上涨 {{ row.comparison.change_percent }}%</text>
          <text>本期 {{ row.comparison.current.weighted_average_unit_price }}/{{ row.base_unit }}；基期 {{ row.comparison.base.weighted_average_unit_price }}/{{ row.base_unit }}</text>
          <text class="muted">采购记录：本期 {{ row.comparison.current.purchase_ids.join('、') || '无' }}；基期 {{ row.comparison.base.purchase_ids.join('、') || '无' }}</text>
        </button>
      </view>
      <view class="skipped"><text class="section-title">无法比较 · {{ skippedRows.reduce((sum, [, count]) => sum + count, 0) }} 组</text><text v-if="!skippedRows.length" class="muted">无。</text><text v-for="[reason, count] in skippedRows" :key="reason">{{ statusLabels[reason] || reason }}：{{ count }} 组</text></view>
      <view class="footnotes"><text>未纳入统计 {{ report.excluded.total }} 条：缺日期 {{ report.excluded.undated }} · 单位未知 {{ report.excluded.unit_unknown }} · 待补箱规 {{ report.excluded.pending_pack_spec }} · 单位不兼容 {{ report.excluded.incompatible_unit }} · 其他币种 {{ report.excluded.other_currency }}</text><text v-for="(note, index) in report.notes" :key="index">{{ note }}</text></view>
    </template>
  </view>
</template>

<style scoped>
.alerts-panel, .alert-results, .skipped, .footnotes { display:flex; flex-direction:column; gap:12px; min-width:0; }
.alerts-panel { padding:18px; overflow-wrap:anywhere; }
.section-title { font-size:16px; font-weight:700; }
.muted { color:var(--muted); font-size:13px; }
.error { color:var(--error); }
.notice { color:var(--green); }
.controls { display:flex; gap:12px; align-items:end; flex-wrap:wrap; }
.controls label { display:flex; flex-direction:column; gap:5px; font-size:14px; }
.controls .toggle { flex-direction:row; align-items:center; min-height:44px; }
.controls input[type=text] { max-width:150px; }
.preview { padding-top:10px; border-top:1px solid var(--line); }
.alert-results, .skipped, .footnotes { padding-top:12px; border-top:1px solid var(--line); }
.alert-row { display:flex; flex-direction:column; align-items:flex-start; gap:5px; padding:12px; text-align:left; background:#f2f7f2; border:1px solid var(--line); border-radius:6px; white-space:normal; }
.alert-row::after, .link-button::after { border:0; }
.link-button { border:0; background:none; color:var(--green); }
@media (max-width:650px) { .alerts-panel { padding:14px; } .controls { align-items:stretch; } .controls label, .controls button { width:100%; } .controls input[type=text] { max-width:none; } }
</style>
