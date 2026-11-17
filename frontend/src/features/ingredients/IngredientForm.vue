<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ApiError, createFoodItem, errorMessage, getPriceUnits, updateFoodItem } from '../../shared/api'
import type { Category, FoodItem } from '../../shared/api'

const props = defineProps<{ item?: FoodItem | null; categories: Category[]; existingActionLabel?: string }>()
const emit = defineEmits<{ saved: [item: FoodItem]; cancel: []; openExisting: [id: number] }>()
const unitGroups = ref<Array<{ name: string; units: Array<{ unit: string }> }>>([])
const unitsLoading = ref(true)
const unitsError = ref('')
const name = ref(props.item?.name ?? '')
const nameZh = ref(props.item?.name_zh ?? '')
const specification = ref(props.item?.specification ?? '')
const categoryId = ref(props.item?.category.id ?? 0)
const baseUnit = ref(props.item?.base_unit ?? '')
const showMore = ref(Boolean(props.item?.name_zh || props.item?.specification))
const openMenu = ref<'category' | 'unit' | null>(null)
const saving = ref(false)
const formError = ref('')
const fieldErrors = ref<Record<string, string>>({})
const existing = ref<{ id: number; name: string; name_zh: string | null } | null>(null)
const categoryLabel = computed(() => props.categories.find(category => category.id === categoryId.value)?.name_zh
  || props.categories.find(category => category.id === categoryId.value)?.name || '选择分类')

async function loadUnits() {
  unitsLoading.value = true
  unitsError.value = ''
  try {
    const catalog = await getPriceUnits()
    unitGroups.value = catalog.dimensions
  } catch (error) {
    unitsError.value = errorMessage(error)
  } finally {
    unitsLoading.value = false
  }
}
onMounted(() => void loadUnits())

function toggleMenu(menu: 'category' | 'unit') {
  openMenu.value = openMenu.value === menu ? null : menu
}

function selectCategory(id: number) {
  categoryId.value = id
  openMenu.value = null
  delete fieldErrors.value.category_id
}

function selectUnit(unit: string) {
  baseUnit.value = unit
  openMenu.value = null
  delete fieldErrors.value.base_unit
}

function validate() {
  const errors: Record<string, string> = {}
  if (!name.value.trim()) errors.name = '请填写英文名。'
  if (!categoryId.value) errors.category_id = '请选择分类。'
  if (!baseUnit.value || !unitGroups.value.some(group => group.units.some(unit => unit.unit === baseUnit.value))) errors.base_unit = '请选择单位目录中的基础单位。'
  fieldErrors.value = errors
  return Object.keys(errors).length === 0
}

async function save() {
  formError.value = ''
  existing.value = null
  if (!validate() || saving.value) return
  saving.value = true
  try {
    const data = {
      name: name.value.trim(),
      category_id: categoryId.value,
      base_unit: baseUnit.value,
      name_zh: nameZh.value.trim() || null,
      specification: specification.value.trim() || null
    }
    const saved = props.item
      ? await updateFoodItem(props.item.id, data)
      : await createFoodItem(data)
    emit('saved', saved)
  } catch (error) {
    if (error instanceof ApiError) {
      const field = error.details?.field
      if (typeof field === 'string' && ['name', 'name_zh', 'category_id', 'base_unit', 'specification'].includes(field)) {
        fieldErrors.value = { ...fieldErrors.value, [field]: error.message }
      } else {
        formError.value = error.message
      }
      const candidate = error.details?.existing
      if (error.status === 409 && candidate && typeof candidate === 'object' && 'id' in candidate && typeof candidate.id === 'number') {
        existing.value = {
          id: candidate.id,
          name: 'name' in candidate && typeof candidate.name === 'string' ? candidate.name : `#${candidate.id}`,
          name_zh: 'name_zh' in candidate && typeof candidate.name_zh === 'string' ? candidate.name_zh : null
        }
      }
    } else {
      formError.value = errorMessage(error)
    }
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <view class="form-fields">
    <text class="form-title">{{ item ? '编辑食材' : '新增食材' }}</text>
    <view class="field">
      <text class="field-label">英文名 *</text>
      <input v-model="name" class="ui-input" aria-label="英文名" placeholder="例如 Chicken Breast" />
      <text v-if="fieldErrors.name" class="field-error" role="alert">{{ fieldErrors.name }}</text>
    </view>
    <view class="field">
      <text class="field-label">分类 *</text>
      <button class="ui-input select-button" aria-label="选择分类" :aria-expanded="openMenu === 'category'" @click="toggleMenu('category')">{{ categoryLabel }} <text aria-hidden="true">⌄</text></button>
      <view v-if="openMenu === 'category'" class="option-list ui-card">
        <button v-for="category in categories" :key="category.id" class="option" @click="selectCategory(category.id)">{{ category.name_zh || category.name }}</button>
      </view>
      <text v-if="fieldErrors.category_id" class="field-error" role="alert">{{ fieldErrors.category_id }}</text>
    </view>
    <view class="field">
      <text class="field-label">基础单位 *</text>
      <button class="ui-input select-button" aria-label="选择基础单位" :aria-expanded="openMenu === 'unit'" :disabled="unitsLoading || !!unitsError" @click="toggleMenu('unit')">{{ baseUnit || (unitsLoading ? '正在加载单位…' : '选择基础单位') }} <text aria-hidden="true">⌄</text></button>
      <view v-if="openMenu === 'unit'" class="option-list ui-card unit-options">
        <view v-for="group in unitGroups" :key="group.name">
          <text class="unit-group">{{ group.name }}</text>
          <button v-for="unit in group.units" :key="unit.unit" class="option" @click="selectUnit(unit.unit)">{{ unit.unit }}</button>
        </view>
      </view>
      <view v-if="unitsError" class="field-error" role="alert">{{ unitsError }} <button class="more-button" @click="loadUnits">重试加载单位</button></view>
      <text v-if="fieldErrors.base_unit" class="field-error" role="alert">{{ fieldErrors.base_unit }}</text>
    </view>
    <button class="more-button" :aria-expanded="showMore" @click="showMore = !showMore">{{ showMore ? '收起更多信息' : '更多信息：中文名与规格' }}</button>
    <template v-if="showMore">
      <view class="field">
        <text class="field-label">中文名</text>
        <input v-model="nameZh" class="ui-input" aria-label="中文名" placeholder="可选" />
        <text v-if="fieldErrors.name_zh" class="field-error" role="alert">{{ fieldErrors.name_zh }}</text>
      </view>
      <view class="field">
        <text class="field-label">规格</text>
        <input v-model="specification" class="ui-input" aria-label="规格" placeholder="可选" />
        <text v-if="fieldErrors.specification" class="field-error" role="alert">{{ fieldErrors.specification }}</text>
      </view>
    </template>
    <text v-if="formError" class="field-error" role="alert">{{ formError }}</text>
    <view v-if="existing" class="existing-card" role="status">
      <text>已有食材：{{ existing.name }}{{ existing.name_zh ? `（${existing.name_zh}）` : '' }}</text>
      <button class="ui-button secondary" @click="emit('openExisting', existing.id)">{{ existingActionLabel || '打开已有食材' }}</button>
    </view>
    <view class="form-actions">
      <button class="ui-button secondary" :disabled="saving" @click="emit('cancel')">取消</button>
      <button class="ui-button" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存食材' }}</button>
    </view>
  </view>
</template>

<style scoped>
.form-fields { display: flex; flex-direction: column; gap: 16px; }
.form-title { font-size: 22px; font-weight: 700; }
.field { position: relative; display: flex; flex-direction: column; gap: 6px; }
.field-label { font-size: 14px; font-weight: 650; }
.field-error { color: var(--error); font-size: 13px; line-height: 1.45; }
.select-button { display: flex; align-items: center; justify-content: space-between; margin: 0; text-align: left; font-weight: 400; cursor: pointer; }
.select-button::after, .option::after, .more-button::after { border: 0; }
.option-list { position: absolute; z-index: 12; top: 76px; left: 0; right: 0; max-height: 220px; overflow-y: auto; padding: 4px; box-shadow: 0 8px 22px #173b2e26; }
.option { display: block; width: 100%; min-height: 44px; margin: 0; padding: 9px 12px; border: 0; border-radius: 6px; background: #fff; color: var(--ink); text-align: left; cursor: pointer; }
.option:hover { background: #edf3eb; }
.unit-group { display: block; padding: 8px 12px 4px; color: var(--muted); font-size: 12px; font-weight: 700; }
.more-button { align-self: start; min-height: 44px; margin: 0; padding: 8px 0; border: 0; background: transparent; color: var(--green); text-align: left; cursor: pointer; }
.existing-card { display: flex; flex-direction: column; align-items: start; gap: 8px; padding: 12px; border: 1px solid #c9dccb; border-radius: 8px; background: #edf3eb; font-size: 14px; }
.form-actions { display: flex; justify-content: flex-end; gap: 10px; }
</style>
