<script setup lang="ts">
/**
 * 识别结果：一张单据的所有行，一屏看完。
 *
 * 这是审核页的主角，F12 之后的重排把它放在最前面。原来这里只有一列
 * 「第 N 行 · 描述」，识别出来的数量/单价/金额**一个都看不见**——它们躺在每行
 * 展开后的草稿表单和更下面的「原文整行」里。老板上传完一张读得完全正确的单据，
 * 第一眼看到的是单据头、识别任务、行状态、对账四张卡片，唯独没有识别结果。
 *
 * 所以这里直接把值摆出来：认出来的照原样显示，没认出来的用橙色「待填」占位，
 * 绝不显示 0（0 在这个产品里是「赠品 / 0 元行」的确定含义）。模型读出来的值带
 * 虚线下划线，提醒「这是机器读的，核一眼」——它与人填过的值在显示上必须分开，
 * 否则「两个模型一致地读错」就会被当成已核实的事实。
 */
import { computed } from 'vue'
import type { Invoice, InvoiceLine } from '../../shared/api'

const props = defineProps<{
  invoice: Invoice
  selectedLineId: number | null
  narrow: boolean
  canManage: boolean
  addingLine: boolean
}>()
const emit = defineEmits<{ select: [line: InvoiceLine]; addLine: [] }>()

const statusLabel: Record<InvoiceLine['status'], string> = {
  pending: '待核对', confirmed: '已入账', skipped: '已跳过'
}

/** 一个格子：显示什么、是不是还空着。 */
interface Cell { text: string; empty: boolean }

function cell(value: string | null): Cell {
  if (value === null || value === '') return { text: '待填', empty: true }
  return { text: value, empty: false }
}

interface Row {
  line: InvoiceLine
  description: Cell
  quantity: Cell
  unit: Cell
  unitPrice: Cell
  amount: Cell
  foodItem: Cell
}

const rows = computed<Row[]>(() => props.invoice.lines.map((line) => ({
  line,
  description: cell(line.description),
  quantity: cell(line.quantity),
  unit: cell(line.purchase_unit),
  unitPrice: cell(line.unit_price),
  amount: cell(line.line_amount),
  // Extraction may have selected a food item; the expanded row explains why.
  foodItem: line.food_item
    ? { text: line.food_item.name, empty: false }
    : line.line_kind === 'adjustment'
      ? { text: '调整行无需食材', empty: false }
      : { text: '待选', empty: true }
})))

const counts = computed(() => props.invoice.reconciliation)
const handwritten = computed(() => props.invoice.recognition.raw_document_fields?.doc_class === 'handwritten_scan')
function automaticReason(line: InvoiceLine): string {
  if (line.food_item?.id !== line.food_item_match?.food_item_id) return ''
  switch (line.food_item_match?.reason) {
    case 'description_exact': return '历史描述自动选中'
    case 'keyword_exact': return '商品名自动认出'
    case 'keyword_created': return '系统新建并选中'
    default: return ''
  }
}
</script>

<template>
  <view class="ui-card section" role="region" aria-label="识别结果">
    <view class="section-heading">
      <text class="section-title">识别结果</text>
      <button v-if="canManage" class="ui-button secondary" :disabled="addingLine" @click="emit('addLine')">
        {{ addingLine ? '正在加…' : '加一行' }}
      </button>
    </view>

    <text class="counts" role="status" aria-live="polite">
      共 {{ counts.line_count }} 行 · 待核对 {{ counts.pending_count }} · 已入账 {{ counts.confirmed_count }}
      <text v-if="counts.skipped_count"> · 已跳过 {{ counts.skipped_count }}</text>
    </text>

    <template v-if="rows.length">
      <view
        v-for="row in rows"
        :key="row.line.id"
        class="line-wrap"
        :class="{ active: selectedLineId === row.line.id, [row.line.status]: true }">
        <button
          class="line"
          :aria-expanded="selectedLineId === row.line.id"
          @click="emit('select', row.line)">
          <view class="line-top">
            <text class="no">{{ row.line.line_no }}</text>
            <text class="desc" :class="{ empty: row.description.empty }">{{ row.description.text }}</text>
            <text class="state">{{ statusLabel[row.line.status] }}</text>
            <text class="chevron" aria-hidden="true">{{ selectedLineId === row.line.id ? '▲' : '▼' }}</text>
          </view>
          <view class="line-figures">
            <text :class="{ empty: row.quantity.empty }">{{ row.quantity.text }}</text>
            <text :class="{ empty: row.unit.empty }">{{ row.unit.text }}</text>
            <text class="sep">×</text>
            <text :class="{ empty: row.unitPrice.empty }">{{ row.unitPrice.text }}</text>
            <text class="sep">=</text>
            <text class="amount" :class="{ empty: row.amount.empty }">{{ row.amount.text }}</text>
            <text class="sep">·</text>
            <text class="food" :class="{ empty: row.foodItem.empty }">食材 {{ row.foodItem.text }}</text>
            <text v-if="automaticReason(row.line)" class="source-badge">{{ automaticReason(row.line) }}</text>
            <text v-if="row.line.food_item?.needs_review" class="review-badge">待确认</text>
            <text v-if="!row.line.food_item && row.line.food_item_match?.reason === 'needs_base_unit'" class="source-badge">请核对采购单位</text>
            <text v-if="!row.line.food_item && handwritten" class="source-badge">手写件不自动预填</text>
          </view>
        </button>
        <view v-if="selectedLineId === row.line.id" class="line-body">
          <slot name="editor" :line="row.line" />
          <button class="collapse" @click="emit('select', row.line)">收起第 {{ row.line.line_no }} 行</button>
        </view>
      </view>

    </template>
    <text v-else class="muted">这张单据还没有行。点「加一行」开始录入。</text>
  </view>
</template>

<style scoped>
.section { padding: 16px; display: flex; flex-direction: column; gap: 10px; }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.section-title { font-size: 17px; font-weight: 700; }
.counts { color: var(--muted); font-size: 14px; }
/* 一行 = 可点的摘要头 + 展开后的草稿表单，边框画在外层，于是展开的表单看起来
   就长在这一行里，而不是飘在下面的另一张卡。 */
.line-wrap { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.line-wrap + .line-wrap { margin-top: 6px; }
.line-wrap.active { border-color: var(--green); }
.line-wrap.skipped { opacity: .7; }
.line {
  display: flex; flex-direction: column; gap: 4px; width: 100%; margin: 0; padding: 10px 12px;
  border: 0; border-radius: 0; background: transparent;
  font-size: 15px; text-align: left; line-height: 1.4; cursor: pointer;
}
.line::after { border: 0; }
.line-wrap.active .line { background: #f1f7f3; }
.line-body { padding: 0 12px 12px; border-top: 1px solid var(--line); }
.collapse {
  display: block; width: 100%; min-height: 44px; margin: 10px 0 0; padding: 10px;
  border: 1px solid var(--line); border-radius: 8px; background: transparent;
  color: var(--green); font-size: 14px; cursor: pointer;
}
.collapse::after { border: 0; }
.chevron { flex: none; color: var(--muted); font-size: 11px; }
.line-top { display: flex; align-items: baseline; gap: 8px; width: 100%; }
.no { flex: none; min-width: 16px; color: var(--muted); font-size: 13px; }
.desc { flex: 1 1 auto; min-width: 0; font-weight: 600; overflow-wrap: break-word; }
.state { flex: none; color: var(--muted); font-size: 13px; }
.line-wrap.confirmed .state { color: var(--green); font-weight: 600; }
.line-figures {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px;
  padding-left: 24px; color: var(--ink); font-size: 14px;
}
.amount { font-weight: 600; }
.food { color: var(--muted); }
.source-badge, .review-badge { padding: 2px 6px; border-radius: 4px; background: #edf3eb; color: #155c3f; font-size: 12px; }
.review-badge { background: #fff0d9; color: #704007; font-weight: 700; }
.sep { color: var(--muted); }
.empty { color: var(--warning); font-style: italic; }
</style>
