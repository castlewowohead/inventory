<script setup lang="ts">
import { ref } from 'vue'
import { ApiError, createCategory, deleteCategory, errorMessage, updateCategory } from '../../shared/api'
import type { Category } from '../../shared/api'

defineProps<{ categories: Category[] }>()
const emit = defineEmits<{ changed: []; close: [] }>()
const newName = ref('')
const newNameZh = ref('')
const editingId = ref<number | null>(null)
const editName = ref('')
const editNameZh = ref('')
const confirmDeleteId = ref<number | null>(null)
const busy = ref(false)
const notice = ref('')
const nameError = ref('')
const otherError = ref('')
const rowError = ref<Record<number, string>>({})

function startEdit(category: Category) {
  editingId.value = category.id
  editName.value = category.name
  editNameZh.value = category.name_zh ?? ''
  confirmDeleteId.value = null
  delete rowError.value[category.id]
}

function failure(error: unknown, id?: number) {
  const message = errorMessage(error)
  if (id !== undefined) rowError.value = { ...rowError.value, [id]: message }
  else if (error instanceof ApiError && error.details?.field === 'name') nameError.value = message
  else otherError.value = message
}

async function add() {
  nameError.value = ''
  otherError.value = ''
  notice.value = ''
  if (!newName.value.trim()) {
    nameError.value = '请填写分类英文名。'
    return
  }
  if (busy.value) return
  busy.value = true
  try {
    await createCategory({ name: newName.value.trim(), name_zh: newNameZh.value.trim() || null })
    newName.value = ''
    newNameZh.value = ''
    notice.value = '分类已新增。'
    emit('changed')
  } catch (error) {
    failure(error)
  } finally {
    busy.value = false
  }
}

async function saveEdit() {
  const id = editingId.value
  if (id === null || busy.value) return
  if (!editName.value.trim()) {
    rowError.value = { ...rowError.value, [id]: '请填写分类英文名。' }
    return
  }
  busy.value = true
  delete rowError.value[id]
  try {
    await updateCategory(id, { name: editName.value.trim(), name_zh: editNameZh.value.trim() || null })
    editingId.value = null
    notice.value = '分类已更新。'
    emit('changed')
  } catch (error) {
    failure(error, id)
  } finally {
    busy.value = false
  }
}

async function remove(id: number) {
  if (busy.value) return
  busy.value = true
  delete rowError.value[id]
  notice.value = ''
  try {
    await deleteCategory(id)
    confirmDeleteId.value = null
    notice.value = '分类已删除。'
    emit('changed')
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      const count = error.details?.food_item_count
      rowError.value = { ...rowError.value, [id]: typeof count === 'number'
        ? `仍有 ${count} 种食材使用这个分类，请先为它们更换分类。`
        : error.message }
    } else failure(error, id)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <view class="manager">
    <view class="manager-head"><text class="manager-title">分类管理</text><button class="text-button" @click="emit('close')">关闭</button></view>
    <view class="add-card ui-card">
      <text class="section-label">新增分类</text>
      <view class="field"><text>英文名 *</text><input v-model="newName" class="ui-input" aria-label="新分类英文名" placeholder="例如 Bakery" /><text v-if="nameError" class="field-error" role="alert">{{ nameError }}</text></view>
      <view class="field"><text>中文名</text><input v-model="newNameZh" class="ui-input" aria-label="新分类中文名" placeholder="可选" /></view>
      <text v-if="otherError" class="field-error" role="alert">{{ otherError }}</text>
      <button class="ui-button" :disabled="busy" @click="add">新增分类</button>
    </view>
    <text v-if="notice" class="notice" role="status">{{ notice }}</text>
    <view v-for="category in categories" :key="category.id" class="category-row ui-card">
      <template v-if="editingId === category.id">
        <view class="field"><text>英文名 *</text><input v-model="editName" class="ui-input" :aria-label="`编辑 ${category.name} 英文名`" /></view>
        <view class="field"><text>中文名</text><input v-model="editNameZh" class="ui-input" :aria-label="`编辑 ${category.name} 中文名`" /></view>
        <view class="row-actions"><button class="ui-button secondary" :disabled="busy" @click="editingId = null">取消</button><button class="ui-button" :disabled="busy" @click="saveEdit">保存分类</button></view>
      </template>
      <template v-else>
        <view class="category-name"><text>{{ category.name_zh || category.name }}</text><text v-if="category.name_zh" class="muted">{{ category.name }}</text></view>
        <view class="row-actions"><button class="ui-button secondary" :disabled="busy" @click="startEdit(category)">改名</button><button class="ui-button ghost danger" :disabled="busy" @click="confirmDeleteId = category.id">删除</button></view>
      </template>
      <text v-if="rowError[category.id]" class="field-error" role="alert">{{ rowError[category.id] }}</text>
      <view v-if="confirmDeleteId === category.id" class="delete-check">
        <text>确定删除「{{ category.name_zh || category.name }}」？如果还有食材属于这个分类，需要先调整这些食材。</text>
        <view class="row-actions"><button class="ui-button secondary" @click="confirmDeleteId = null">取消</button><button class="ui-button danger-solid" :disabled="busy" @click="remove(category.id)">确认删除</button></view>
      </view>
    </view>
  </view>
</template>

<style scoped>
.manager { display: flex; flex-direction: column; gap: 12px; }
.manager-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.manager-title { font-size: 22px; font-weight: 700; }
.add-card, .category-row { display: flex; flex-direction: column; gap: 12px; padding: 16px; }
.section-label { font-weight: 700; }
.field { display: flex; flex-direction: column; gap: 6px; font-size: 14px; }
.field-error { color: var(--error); font-size: 13px; line-height: 1.45; }
.notice { color: var(--green); font-size: 14px; }
.category-name { display: flex; flex-direction: column; gap: 4px; font-weight: 650; }
.category-name .muted { font-size: 13px; font-weight: 400; }
.row-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.row-actions .ui-button { min-height: 44px; padding: 8px 12px; font-size: 14px; }
.delete-check { display: flex; flex-direction: column; gap: 10px; padding: 12px; border-radius: 8px; background: #fff4e6; font-size: 14px; }
.danger { color: var(--error); }
.danger-solid { background: var(--error); border-color: var(--error); }
.text-button { min-height: 44px; padding: 8px; margin: 0; border: 0; background: transparent; color: var(--green); cursor: pointer; }
.text-button::after { border: 0; }
</style>
