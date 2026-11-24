<script setup lang="ts">
/**
 * A date typed into a fixed shape: `年 - 月 - 日`, with the dashes already there.
 *
 * It replaces the plain `YYYY-MM-DD` text box, which made the user responsible
 * for the separators and silently rejected `2026/6/3` or `6-3-2026`
 * (`docs/multifiles.md` 第 11 节 ②). Here each part is its own box, so there is
 * nothing to format and nothing to get wrong about the order.
 *
 * ⚠️ It emits an ISO date **only when all three boxes are filled**, and `''`
 * otherwise. The caller keeps its own validity check: an empty string is "not
 * finished", not "invalid", and the caller's button stays disabled either way.
 * `2026-02-31` is still the caller's to reject — this control only fixes shape.
 */
import { ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  modelValue: string
  disabled?: boolean
  label?: string
}>(), { disabled: false, label: '日期' })
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const year = ref('')
const month = ref('')
const day = ref('')
/** Which box the next keystroke should land in, after one fills up. */
const focused = ref<'year' | 'month' | 'day' | null>(null)

function split(value: string) {
  const match = /^(\d{0,4})-?(\d{0,2})-?(\d{0,2})$/.exec(value ?? '')
  year.value = match?.[1] ?? ''
  month.value = match?.[2] ?? ''
  day.value = match?.[3] ?? ''
}
split(props.modelValue)
watch(() => props.modelValue, (value) => {
  if (value !== join()) split(value)
})

function join(): string {
  if (!year.value || !month.value || !day.value) return ''
  return `${year.value.padStart(4, '0')}-${month.value.padStart(2, '0')}-${day.value.padStart(2, '0')}`
}

/** uni-app H5 puts the text on `detail`; a plain DOM event has it on `target`. */
function typed(event: Event): string {
  const detail = (event as Event & { detail?: { value?: string } }).detail
  return detail?.value ?? (event.target as HTMLInputElement | null)?.value ?? ''
}

/** Digits only: a pasted `2026/06/03` fills the three boxes instead of failing. */
function edit(part: 'year' | 'month' | 'day', event: Event) {
  const size = part === 'year' ? 4 : 2
  const digits = typed(event).replace(/\D/g, '')
  if (part === 'year') year.value = digits.slice(0, size)
  else if (part === 'month') month.value = digits.slice(0, size)
  else day.value = digits.slice(0, size)
  if (digits.length >= size) focused.value = part === 'year' ? 'month' : part === 'month' ? 'day' : null
  emit('update:modelValue', join())
}

/** `6` means June; pad on the way out so the value is always a real ISO date. */
function pad(part: 'month' | 'day') {
  const box = part === 'month' ? month : day
  if (box.value.length === 1) box.value = box.value.padStart(2, '0')
  emit('update:modelValue', join())
}
</script>

<template>
  <view class="date-field" role="group" :aria-label="label">
    <input class="box year" type="number" :value="year" :disabled="disabled"
      placeholder="YYYY" maxlength="4" :focus="focused === 'year'"
      :aria-label="`${label} · 年`" @input="edit('year', $event)" />
    <text class="dash" aria-hidden="true">-</text>
    <input class="box part" type="number" :value="month" :disabled="disabled"
      placeholder="MM" maxlength="2" :focus="focused === 'month'"
      :aria-label="`${label} · 月`" @input="edit('month', $event)" @blur="pad('month')" />
    <text class="dash" aria-hidden="true">-</text>
    <input class="box part" type="number" :value="day" :disabled="disabled"
      placeholder="DD" maxlength="2" :focus="focused === 'day'"
      :aria-label="`${label} · 日`" @input="edit('day', $event)" @blur="pad('day')" />
  </view>
</template>

<style scoped>
.date-field { display: flex; align-items: center; gap: 6px; }
.box { min-height: 44px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: #fff; color: var(--ink); font-size: 16px; text-align: center; }
.box.year { width: 88px; }
.box.part { width: 62px; }
.dash { color: var(--muted); font-size: 18px; }
</style>
