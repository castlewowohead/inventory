<script setup lang="ts">
/**
 * One card on the organizing board (M10): a PDF/image page or a spreadsheet row.
 *
 * The card carries three visually separate things, and the separation is the
 * point (`docs/multifiles.md` section 4):
 *
 * - **已确认** — what a person decided. Solid chip.
 * - **建议** — a candidate and the reason it was suggested. Outlined chip, and
 *   nothing on this card ever turns one into a decision; the user moves the
 *   card to confirm it.
 * - **还缺什么 / 读到什么** — plain wording, so an empty supplier does not read
 *   as a failed one.
 *
 * Operations stay thin on purpose: a tap on the body opens the original, the
 * grip drags, and everything else is behind 更多.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import type { IntakeItem } from '../../shared/api'
import { attachDragHandle } from '../../shared/platform/dragBoard'
import {
  cardLocation, decidedOf, isDemoReading, isHandedOff, missingLabel, monthDayLabel,
  recognitionNote, suggestionsOf, yearLabel
} from './intakeBoard'

const props = defineProps<{
  item: IntakeItem
  selected: boolean
  /** False on platforms without pointer dragging; the grip is then left out. */
  draggable: boolean
}>()
const emit = defineEmits<{ toggle: []; preview: []; menu: []; drag: [event: unknown] }>()

const decided = computed(() => decidedOf(props.item))
const confirmedText = computed(() => {
  const parts: string[] = []
  if (decided.value.date) {
    const year = yearLabel(decided.value.date)
    parts.push(`${monthDayLabel(decided.value.date)}${year ? `（${year}）` : ''}`)
  }
  if (decided.value.source) parts.push(decided.value.source)
  return parts.join(' · ')
})
const missing = computed(() => missingLabel(props.item))
const suggestions = computed(() => suggestionsOf(props.item))
const note = computed(() => recognitionNote(props.item))
const kindLabel = computed(() => (props.item.kind === 'page' ? '图片/PDF 页' : '表格行'))
/** Already in review: the organizer cannot move it, and the card says that. */
const handedOff = computed(() => isHandedOff(props.item))

/**
 * The grip's `pointerdown` has to be a real DOM listener: `<view>` drops
 * template listeners it does not know (see `dragBoard.ts`). Re-attached when
 * the grip comes or goes, e.g. after the batch is closed.
 */
const grip = ref<unknown>(null)
let detachGrip = () => {}
function bindGrip() {
  detachGrip()
  detachGrip = attachDragHandle(grip.value, (event) => emit('drag', event))
}
onMounted(bindGrip)
watch(() => props.draggable, bindGrip, { flush: 'post' })
onUnmounted(() => detachGrip())
</script>

<template>
  <view class="card" :class="{ selected, 'handed-off': handedOff }">
    <button class="pick" :aria-pressed="selected ? 'true' : 'false'"
      :aria-label="`${selected ? '取消选中' : '选中'} ${item.original_name} ${cardLocation(item)}`"
      @click="emit('toggle')">{{ selected ? '✓' : '' }}</button>
    <view class="card-main" @click="emit('preview')">
      <text class="card-title">{{ item.original_name }}</text>
      <text class="card-meta muted">{{ cardLocation(item) }} · {{ kindLabel }}</text>
      <view class="chips">
        <text v-if="confirmedText" class="chip confirmed">已确认 · {{ confirmedText }}</text>
        <text v-if="missing" class="chip missing">{{ missing }}</text>
        <text v-for="chip in suggestions" :key="chip.id" class="chip suggest">
          建议 {{ chip.text }} · {{ chip.basis }}
        </text>
        <text v-if="handedOff" class="chip handed">已送审</text>
        <text v-if="isDemoReading(item)" class="chip demo">演示识别结果</text>
      </view>
      <text v-if="handedOff" class="card-note muted">
        这一项已经进入单据审核，整理页不能再移动它；要改日期或商家，请在审核里修改分类。
      </text>
      <text v-if="note" class="card-note muted">{{ note }}</text>
    </view>
    <view class="card-side">
      <view v-if="draggable && !handedOff" ref="grip" class="grip" role="img" :aria-label="`拖动 ${cardLocation(item)}`">⠿</view>
      <button class="ui-button ghost more" :aria-label="`${item.original_name} ${cardLocation(item)} 的更多操作`"
        @click="emit('menu')">更多</button>
    </view>
  </view>
</template>

<style scoped>
.card { display: flex; align-items: flex-start; gap: 10px; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
.card.selected { border-color: var(--green); box-shadow: inset 0 0 0 1px var(--green); background: #f3f8f3; }
.pick { width: 44px; height: 44px; flex: none; margin: 0; padding: 0; border: 1px solid #aebeb3; border-radius: 7px; background: #fff; color: var(--green); font-size: 18px; font-weight: 700; line-height: 1; cursor: pointer; }
.pick::after { border: 0; }
.card.selected .pick { border-color: var(--green); background: #e6f1e9; }
.card.handed-off { background: #f7f9fb; border-color: #b7c8de; }
.card-main { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 4px; cursor: pointer; }
.card-title { font-size: 14px; font-weight: 650; overflow-wrap: anywhere; }
.card-meta { font-size: 12px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { padding: 2px 7px; border-radius: 5px; font-size: 12px; line-height: 1.5; }
/* Solid = decided by a person. Dashed = still only a suggestion. */
.chip.confirmed { background: var(--green); color: #fff; font-weight: 650; }
.chip.missing { background: #fff5e9; color: #704007; border: 1px solid #e3c9a6; font-weight: 650; }
.chip.suggest { background: #fff; color: var(--muted); border: 1px dashed #aebeb3; }
.chip.demo { background: #fff5e9; color: #704007; font-weight: 700; }
.chip.handed { background: #e7eef7; color: #1f3b63; border: 1px solid #b7c8de; font-weight: 650; }
.card-note { font-size: 12px; line-height: 1.5; }
.card-side { flex: none; display: flex; flex-direction: column; align-items: center; gap: 4px; }
/* `touch-action: none` on this grip only: a finger anywhere else on the card
   still scrolls the list, which is why dragging never steals the scroll. */
.grip { width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; border: 1px solid var(--line); border-radius: 7px; color: var(--muted); font-size: 18px; cursor: grab; touch-action: none; user-select: none; }
.more { min-width: 44px; padding: 4px 6px; font-size: 12px; }
</style>
