<script setup lang="ts">
/**
 * One card on the organizing board (M10): a PDF/image page or a spreadsheet row.
 *
 * The card carries three visually separate things, and the separation is the
 * point (`docs/multifiles.md` section 4):
 *
 * - **已确认** — what a person decided. Solid chip.
 * - **建议** — a candidate and its basis. A date candidate has an explicit
 *   confirm button; the parent performs the same versioned move as drag/drop.
 * - **还缺什么 / 读到什么** — plain wording, so an empty supplier does not read
 *   as a failed one.
 *
 * Operations stay thin on purpose: a tap on the body opens the original, the
 * grip drags, and everything else is behind 更多 — with one exception:
 * **撤回填表 is a button of its own** (2026-11-24 feedback, 第 13 节 ②). It is the
 * only way back out of a draft, so hiding it one tap deeper than the way in
 * (继续填表) made the card look one-way.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import type { IntakeItem } from '../../shared/api'
import { attachDragHandle } from '../../shared/platform/dragBoard'
import {
  cardLocation, decidedOf, formState, isDemoReading, isHandedOff, monthDayLabel,
  recognitionNote, suggestionsOf, yearLabel
} from './intakeBoard'

const props = defineProps<{
  item: IntakeItem
  selected: boolean
  confirmable: boolean
  proposedDate: boolean
  proposedSource: boolean
  /** False on platforms without pointer dragging; the grip is then left out. */
  draggable: boolean
  /**
   * Why this one card cannot start a form on its own, or `''` when it can.
   *
   * The card keeps its own 开始填表 (2026-11-24 feedback, 第 13 节 ⑤) so one
   * invoice does not need 勾选 → 找按钮 → 点. The parent owns the rule, because
   * only it knows which date/supplier group this card is being shown under.
   */
  startProblem: string
}>()
const emit = defineEmits<{
  toggle: []; preview: []; menu: []; drag: [event: unknown]; confirmDate: [date: string]
  /** Back into the draft this card is being filled in (第 11 节 ④). */
  resume: []
  /** Throw that draft away and come back to 分类好、还没开始填 (第 11 节 ③). */
  withdraw: []
  /**
   * 已完成的单据重来一遍：清空它填过的数据，就地开一张新的空草稿（第 20 节）。
   *
   * ⚠️ 与 `withdraw` 的差别是**落点**：撤回填表回到「还没开始填」，重新填写直接
   * 落进那张新的空表。所以它们是两个按钮、两句话，不是同一条路的两种叫法。
   */
  refill: []
  /** Start a form for this one card alone, without touching the selection. */
  start: []
  /**
   * 只改这一张的商家（2026-11-24 反馈，第 17 节 ①）。
   *
   * 组上的「修改商家」改的是**整组的名字**，所以一张放错了商家的纸没有出路：
   * 人工决定压过纸面候选，而卡片上以前只有「移到…」（它连日期一起要）。
   */
  editSource: []
}>()

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
const missing = computed(() => {
  const noDate = !props.item.decided_purchase_date && !props.proposedDate
  const noSource = !props.item.decided_source_name && !props.proposedSource
  if (noDate && noSource) return '缺日期和商家'
  if (noDate) return '缺日期'
  if (noSource) return '缺商家'
  return ''
})
const suggestions = computed(() => suggestionsOf(props.item).filter((chip) =>
  !(chip.field === 'purchase_date' && props.proposedDate)
  && !(chip.field === 'source_name' && props.proposedSource)))
/**
 * 日期是怎么来的，要说得出：同文件里另一页的明确格式（第 8 节），还是本项目
 * 默认的月/日/年（第 15 节）。两者都可在右侧改，但它们不是同一条依据。
 */
const note = computed(() => {
  const reading = props.item.recognition
  if (!props.proposedDate || !reading.date_ambiguous) return recognitionNote(props.item)
  if (reading.date_assumed_order) {
    const habit = reading.date_assumed_order === 'dmy' ? '日/月/年' : '月/日/年'
    return `日期按${habit}这一写法读出，可在右侧修改。`
  }
  return '日期依据同文件的明确日期格式推断，可在右侧修改。'
})
const kindLabel = computed(() => (props.item.kind === 'page' ? '图片/PDF 页' : '表格行'))
/**
 * 编辑中: a draft exists for this card, so the organizer cannot move it.
 *
 * ⚠️ It is called 编辑中 and not 已送审 because the user has not handed anything
 * to anyone — they opened a form and can still walk away from it. The card
 * therefore offers both ways out as buttons on the card: 继续填表 and 撤回填表
 * (第 11 节 ③④; the second one came out of 更多 on 2026-11-24).
 */
const handedOff = computed(() => isHandedOff(props.item))
/** 编辑中 / 已完成 / 草稿已作废 — and what the card offers for each. */
const form = computed(() => formState(props.item))

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
      @click="emit('toggle')"><text class="box">{{ selected ? '✓' : '' }}</text></button>
    <view class="card-main" @click="emit('preview')">
      <text class="card-title">{{ item.original_name }}</text>
      <text class="card-meta muted">{{ cardLocation(item) }} · {{ kindLabel }}</text>
      <view class="chips">
        <text v-if="confirmedText" class="chip confirmed">已确认 · {{ confirmedText }}</text>
        <text v-if="proposedDate || proposedSource" class="chip suggest">系统建议 · 可在右侧修改</text>
        <text v-if="missing" class="chip missing">{{ missing }}</text>
        <template v-for="chip in suggestions" :key="chip.id">
          <button v-if="chip.field === 'purchase_date'" class="chip suggest suggest-action"
            :disabled="!confirmable || handedOff" :aria-label="`确认这张内容的日期为 ${chip.text}，依据：${chip.basis}`"
            @click.stop="emit('confirmDate', chip.value)">
            确认 {{ chip.text }} · {{ chip.basis }}
          </button>
          <text v-else class="chip suggest">建议 {{ chip.text }} · {{ chip.basis }}</text>
        </template>
        <text v-if="form" class="chip handed">{{ form.label }}</text>
        <text v-if="isDemoReading(item)" class="chip demo">演示识别结果</text>
      </view>
      <text v-if="form" class="card-note muted">{{ form.note }}</text>
      <text v-if="note" class="card-note muted">{{ note }}</text>
    </view>
    <view class="card-side">
      <button v-if="form && form.action === 'resume'" class="ui-button resume"
        :aria-label="`继续填写 ${item.original_name} ${cardLocation(item)} 的单据`"
        @click.stop="emit('resume')">继续填表</button>
      <!-- 已完成的卡片：主按钮是「重新填写」（蓝），不再是只能看的「查看单据」。
           它会清空这张单据并撤销它的入账，所以确认仍在对话框里问一次（第 20 节）。 -->
      <button v-else-if="form && form.action === 'refill'" class="ui-button refill"
        :aria-label="`清空并重新填写 ${item.original_name} ${cardLocation(item)} 的单据`"
        @click.stop="emit('refill')">重新填写</button>
      <!-- 还没开始填的卡片：自己就能开始填表，按钮在「拖动」左边（第 13 节 ⑤）。 -->
      <view v-if="!handedOff" class="side-row">
        <button class="ui-button start" :disabled="startProblem !== ''" :title="startProblem"
          :aria-label="`开始填写 ${item.original_name} ${cardLocation(item)} 的单据`"
          @click.stop="emit('start')">开始填表</button>
        <view v-if="draggable" ref="grip" class="grip" role="img" :aria-label="`拖动 ${cardLocation(item)}`">⠿<text class="grip-label">拖动</text></view>
      </view>
      <!-- 撤回填表紧跟在「继续填表」下面：进草稿和出草稿是同一层操作。已完成的
           （action === 'refill'）不给这一个——它那条路是上面的「重新填写」。 -->
      <view class="side-row">
        <button v-if="form && (form.action === 'resume' || form.action === 'withdraw')"
          class="ui-button danger withdraw"
          :aria-label="`撤回 ${item.original_name} ${cardLocation(item)} 的填表`"
          @click.stop="emit('withdraw')">撤回填表</button>
        <!-- 完成之后看一眼那张单据：整理页到它的唯一一条路，所以留着，只是退到
             第二行给「重新填写」让位（用户的话是「绿色按钮变成蓝色的重新填写」）。 -->
        <button v-if="form && form.action === 'refill'" class="ui-button ghost source"
          :aria-label="`查看 ${item.original_name} ${cardLocation(item)} 的单据`"
          @click.stop="emit('resume')">查看单据</button>
        <!-- 只改这一张的商家。放在卡片上而不是「更多」里：组上的「修改商家」一眼
             看得见，单张的那条路不该比它深一层（同第 13 节 ② 的理由）。 -->
        <button v-if="!handedOff" class="ui-button ghost source"
          :aria-label="`修改 ${item.original_name} ${cardLocation(item)} 的商家`"
          @click.stop="emit('editSource')">改商家</button>
        <button class="ui-button ghost more" :aria-label="`${item.original_name} ${cardLocation(item)} 的更多操作`"
          @click="emit('menu')">更多</button>
      </view>
    </view>
  </view>
</template>

<style scoped>
.card { display: flex; align-items: flex-start; gap: 10px; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
.card.selected { border-color: var(--green); box-shadow: inset 0 0 0 1px var(--green); background: #f3f8f3; }
/* 44px 的是**点击区**，看得见的方框只有 22px：原来整块 44px 都画成边框，
   大得不像勾选框，倒像一个空按钮（2026-11-24 反馈，第 13 节 ④）。 */
.pick { width: 44px; height: 44px; flex: none; display: flex; align-items: center; justify-content: center; margin: 0; padding: 0; border: 0; background: transparent; cursor: pointer; }
.pick::after { border: 0; }
.box { width: 22px; height: 22px; display: flex; align-items: center; justify-content: center; border: 2px solid #8aa394; border-radius: 4px; background: #fff; color: transparent; font-size: 15px; font-weight: 800; line-height: 1; }
.card.selected .box { border-color: var(--green); background: var(--green); color: #fff; }
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
.chip.suggest-action { min-height: 40px; margin: 0; text-align: left; cursor: pointer; }
.chip.suggest-action::after { border: 0; }
.chip.suggest-action:disabled { cursor: default; opacity: .55; }
.chip.demo { background: #fff5e9; color: #704007; font-weight: 700; }
.chip.handed { background: #e7eef7; color: #1f3b63; border: 1px solid #b7c8de; font-weight: 650; }
.card-note { font-size: 12px; line-height: 1.5; }
.card-side { flex: none; max-width: 160px; display: flex; flex-direction: column; align-items: stretch; gap: 6px; }
/* 窄屏放不下「撤回填表 + 更多」时让它们换行，而不是把卡片正文挤没。 */
.side-row { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 6px; }
/* `touch-action: none` on this grip only: a finger anywhere else on the card
   still scrolls the list, which is why dragging never steals the scroll. */
.grip { align-self: center; width: 52px; height: 52px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px; border: 1px solid var(--line); border-radius: 7px; color: var(--muted); font-size: 18px; cursor: grab; touch-action: none; user-select: none; }
.grip-label { font-size: 11px; line-height: 1; }
.more { min-width: 44px; padding: 4px 6px; font-size: 12px; }
.source { min-height: 40px; margin: 0; padding: 4px 8px; font-size: 12px; white-space: nowrap; }
/* 红的：它会把这张草稿里已经填的东西清空。真正的确认仍在对话框里问一次。 */
.start { min-height: 40px; margin: 0; padding: 6px 10px; font-size: 12px; font-weight: 650; white-space: nowrap; }
.withdraw { min-height: 40px; margin: 0; padding: 6px 10px; border-color: var(--error); background: var(--error); color: #fff; font-size: 12px; font-weight: 650; white-space: nowrap; }
.resume { min-height: 40px; margin: 0; padding: 6px 10px; font-size: 12px; white-space: nowrap; }
/* 蓝的：它不像绿按钮那样往前走一步，也不像红按钮那样只清空——它清空之后把人直接
   放回一张空表。颜色取的是这张卡片「有草稿」那一族已经在用的蓝（`.chip.handed`）。 */
.refill { min-height: 40px; margin: 0; padding: 6px 10px; border-color: var(--blue); background: var(--blue); color: #fff; font-size: 12px; font-weight: 650; white-space: nowrap; }
</style>
