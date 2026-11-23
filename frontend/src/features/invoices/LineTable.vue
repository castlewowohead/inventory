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
import { computed, ref, watch } from 'vue'
import type { Invoice, InvoiceLine } from '../../shared/api'

const props = defineProps<{
  invoice: Invoice
  selectedLineId: number | null
  narrow: boolean
  canManage: boolean
  addingLine: boolean
}>()
const emit = defineEmits<{ select: [line: InvoiceLine]; addLine: [] }>()
const order = ref<'food' | 'original'>('food')
const showRemoved = ref(false)
watch(() => props.invoice.reconciliation.skipped_count, (next, previous) => {
  if (next > previous) showRemoved.value = false
})

const statusLabel: Record<InvoiceLine['status'], string> = {
  pending: '待核对', confirmed: '已入账', skipped: '已移除'
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
  showCalculation: boolean
}

const rows = computed<Row[]>(() => props.invoice.lines.filter((line) => showRemoved.value || line.status !== 'skipped').map((line) => ({
  line,
  description: line.description ? cell(line.description) : { text: '未填写商品名称', empty: true },
  quantity: cell(line.quantity),
  unit: cell(line.purchase_unit),
  unitPrice: cell(line.unit_price),
  amount: cell(line.line_amount),
  showCalculation: Boolean(line.quantity && line.purchase_unit && line.unit_price && line.line_amount),
  // Extraction may have selected a food item; the expanded row explains why.
  foodItem: line.food_item
    ? { text: line.food_item.name, empty: false }
    : line.line_kind === 'adjustment'
      ? { text: '调整项', empty: false }
      : { text: '待选', empty: true }
})))

type DisplayEntry =
  | { kind: 'heading'; key: string; title: string; count: number; level: 'category' | 'food' }
  | { kind: 'row'; key: string; row: Row }

const nameOrder = new Intl.Collator('zh-CN', { numeric: true })
const byOriginal = (a: Row, b: Row) => a.line.line_no - b.line.line_no

/** Only the display order changes; each row keeps its original number and editor. */
const displayEntries = computed<DisplayEntry[]>(() => {
  if (order.value === 'original') {
    return [...rows.value].sort(byOriginal).map((row) => ({ kind: 'row', key: `line-${row.line.id}`, row }))
  }

  const categories = new Map<number, {
    title: string
    items: Map<number, { title: string; rows: Row[] }>
  }>()
  const adjustments: Row[] = []
  const unassigned: Row[] = []
  for (const row of rows.value) {
    const item = row.line.food_item
    if (!item) {
      if (row.line.line_kind === 'adjustment') adjustments.push(row)
      else unassigned.push(row)
      continue
    }
    let category = categories.get(item.category.id)
    if (!category) {
      category = { title: item.category.name_zh || item.category.name, items: new Map() }
      categories.set(item.category.id, category)
    }
    let group = category.items.get(item.id)
    if (!group) {
      group = { title: item.name_zh || item.name, rows: [] }
      category.items.set(item.id, group)
    }
    group.rows.push(row)
  }

  const entries: DisplayEntry[] = []
  const addRows = (group: Row[]) => {
    for (const row of group.sort(byOriginal)) entries.push({ kind: 'row', key: `line-${row.line.id}`, row })
  }
  for (const [categoryId, category] of [...categories.entries()].sort((a, b) => nameOrder.compare(a[1].title, b[1].title))) {
    const count = [...category.items.values()].reduce((total, item) => total + item.rows.length, 0)
    entries.push({ kind: 'heading', key: `category-${categoryId}`, title: category.title, count, level: 'category' })
    for (const [itemId, item] of [...category.items.entries()].sort((a, b) => nameOrder.compare(a[1].title, b[1].title))) {
      entries.push({ kind: 'heading', key: `food-${itemId}`, title: item.title, count: item.rows.length, level: 'food' })
      addRows(item.rows)
    }
  }
  if (adjustments.length) {
    entries.push({ kind: 'heading', key: 'adjustments', title: '其他费用与折让', count: adjustments.length, level: 'category' })
    addRows(adjustments)
  }
  if (unassigned.length) {
    entries.push({ kind: 'heading', key: 'unassigned', title: '未选择食材', count: unassigned.length, level: 'category' })
    addRows(unassigned)
  }
  return entries
})

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
      <view class="heading-actions">
        <button class="ui-button secondary" @click="order = order === 'food' ? 'original' : 'food'">
          {{ order === 'food' ? '切换为原件排序' : '切换为按食材类型排序' }}
        </button>
        <button v-if="canManage" class="ui-button secondary" :disabled="addingLine" @click="emit('addLine')">
          {{ addingLine ? '正在加…' : '加一行' }}
        </button>
      </view>
    </view>

    <view class="counts-row">
      <text class="counts" role="status" aria-live="polite">
        共 {{ counts.pending_count + counts.confirmed_count }} 行 · 待核对 {{ counts.pending_count }} · 已入账 {{ counts.confirmed_count }}
      </text>
      <button v-if="counts.skipped_count" class="removed-toggle" :aria-expanded="showRemoved" @click="showRemoved = !showRemoved">
        {{ showRemoved ? '收起已移除行' : `查看已移除 ${counts.skipped_count} 行` }}
      </button>
    </view>

    <template v-if="rows.length">
      <template v-for="entry in displayEntries" :key="entry.key">
      <view v-if="entry.kind === 'heading'" class="group-heading" :class="entry.level">
        <text>{{ entry.title }}</text><text class="group-count">{{ entry.count }} 行</text>
      </view>
      <view
        v-else
        class="line-wrap"
        :class="{ active: selectedLineId === entry.row.line.id, grouped: order === 'food', [entry.row.line.status]: true }">
        <button
          class="line"
          :aria-expanded="selectedLineId === entry.row.line.id"
          @click="emit('select', entry.row.line)">
          <view class="line-top">
            <text class="no">{{ entry.row.line.line_no }}</text>
            <text class="desc" :class="{ empty: entry.row.description.empty }">{{ entry.row.description.text }}</text>
            <text class="state">{{ statusLabel[entry.row.line.status] }}</text>
            <text class="chevron" aria-hidden="true">{{ selectedLineId === entry.row.line.id ? '▲' : '▼' }}</text>
          </view>
          <view
            v-if="entry.row.showCalculation || !entry.row.foodItem.empty || automaticReason(entry.row.line) || entry.row.line.food_item?.needs_review || (!entry.row.line.food_item && entry.row.line.food_item_match?.reason === 'needs_base_unit') || (!entry.row.line.food_item && handwritten)"
            class="line-figures">
            <template v-if="entry.row.showCalculation">
              <text>{{ entry.row.quantity.text }}</text>
              <text>{{ entry.row.unit.text }}</text>
              <text class="sep">×</text>
              <text>{{ entry.row.unitPrice.text }}</text>
              <text class="sep">=</text>
              <text class="amount">{{ entry.row.amount.text }}</text>
            </template>
            <text v-if="!entry.row.foodItem.empty" class="food">{{ entry.row.showCalculation ? '· ' : '' }}{{ entry.row.line.line_kind === 'adjustment' ? '调整项' : `食材 ${entry.row.foodItem.text}` }}</text>
            <text v-if="automaticReason(entry.row.line)" class="source-badge">{{ automaticReason(entry.row.line) }}</text>
            <text v-if="entry.row.line.food_item?.needs_review" class="review-badge">待确认</text>
            <text v-if="!entry.row.line.food_item && entry.row.line.food_item_match?.reason === 'needs_base_unit'" class="source-badge">请核对采购单位</text>
            <text v-if="!entry.row.line.food_item && handwritten" class="source-badge">手写件不自动预填</text>
          </view>
        </button>
        <view v-if="selectedLineId === entry.row.line.id" class="line-body">
          <slot name="editor" :line="entry.row.line" />
          <button class="collapse" @click="emit('select', entry.row.line)">收起第 {{ entry.row.line.line_no }} 行</button>
        </view>
      </view>
      </template>

    </template>
    <text v-else class="muted">当前没有需要核对的行。可以点「加一行」继续录入。</text>
  </view>
</template>

<style scoped>
.section { padding: 16px; display: flex; flex-direction: column; gap: 10px; }
.section-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.heading-actions { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 8px; }
.section-title { font-size: 17px; font-weight: 700; }
.counts { color: var(--muted); font-size: 14px; }
.counts-row { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px 12px; }
.removed-toggle { width: auto; min-height: 36px; margin: 0; padding: 4px 0; border: 0; background: transparent; color: var(--green); font-size: 13px; cursor: pointer; }
.removed-toggle::after { border: 0; }
.group-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; padding: 13px 12px 7px; color: var(--ink); font-weight: 700; }
.group-heading.category { margin-top: 8px; border-radius: 6px; background: #f1f6f1; font-size: 16px; }
.group-heading.food { margin-left: 18px; padding-top: 10px; border-bottom: 1px solid var(--line); color: var(--green); font-size: 14px; }
.group-count { flex: none; color: var(--muted); font-size: 12px; font-weight: 400; }
/* 一行 = 可点的摘要头 + 展开后的草稿表单，边框画在外层，于是展开的表单看起来
   就长在这一行里，而不是飘在下面的另一张卡。 */
.line-wrap { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.line-wrap.grouped { margin-left: 36px; }
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
@media (max-width: 899px) {
  .group-heading.food { margin-left: 8px; }
  .line-wrap.grouped { margin-left: 16px; }
}
</style>
