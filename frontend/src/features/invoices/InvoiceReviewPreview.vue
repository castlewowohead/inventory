<script setup lang="ts">
import { reactive, ref } from 'vue'

defineProps<{ invoiceId: number | null }>()
const emit = defineEmits<{ back: [] }>()
const mobilePanel = ref<'document' | 'form'>('form')
const draft = reactive({ description: '', quantity: '', purchaseUnit: '', unitPrice: '', lineAmount: '' })
</script>

<template>
  <view class="review-layout">
    <view class="desktop-heading">
      <view>
        <button class="back-link" @click="emit('back')">← 返回单据</button>
        <text class="review-title">审核布局预览</text>
        <text class="muted review-subtitle">{{ invoiceId === null ? '尚未选择真实单据' : `单据 #${invoiceId} · 尚未读取详情` }}</text>
      </view>
      <text class="preview-badge">开发占位 · 无业务数据</text>
    </view>

    <view class="mobile-tabs" aria-label="审核视图">
      <button :class="{ active: mobilePanel === 'document' }" @click="mobilePanel = 'document'">原件</button>
      <button :class="{ active: mobilePanel === 'form' }" @click="mobilePanel = 'form'">填写</button>
    </view>

    <view class="review-grid">
      <view class="ui-card document-pane" :class="{ 'mobile-hidden': mobilePanel !== 'document' }">
        <view class="pane-heading">
          <view><text class="eyebrow">ORIGINAL / 原件</text><text class="pane-title">原始文件预览</text></view>
          <text class="placeholder">页码待接入</text>
        </view>
        <view class="document-canvas">
          <view class="paper-outline">
            <text class="paper-caption">原件区域</text>
            <text class="placeholder">图片与 PDF 将在 F06 接入</text>
          </view>
        </view>
        <view class="document-foot"><text class="placeholder">未读取文件，不显示伪造的裁图或识别文字。</text></view>
      </view>

      <view class="form-pane" :class="{ 'mobile-hidden': mobilePanel !== 'form' }">
        <view class="ui-card header-card">
          <text class="eyebrow">INVOICE / 单据信息</text>
          <text class="pane-title">来源 · 日期 · 币种 · 总额</text>
          <text class="placeholder">单据头尚未读取；金额与日期不会被预填。</text>
        </view>

        <view class="ui-card row-card">
          <view class="row-heading">
            <view><text class="eyebrow">REVIEW / 当前行</text><text class="pane-title">待核对行 · 布局示意</text></view>
            <text class="row-count">— / — 行</text>
          </view>
          <view class="status-row" aria-label="审核状态示意">
            <text>待核对 · 未读取</text><text>已入账 · 未读取</text><text>跳过 · 未读取</text>
          </view>
          <view class="crop-placeholder">
            <text class="crop-label">当前字段裁图</text>
            <text class="placeholder">待接入 · 请在原件中核对</text>
          </view>
          <view class="fields">
            <view class="field full"><text class="field-label">原始描述</text><input v-model="draft.description" class="ui-input" placeholder="待核对原始描述" /></view>
            <view class="field full"><text class="field-label">标准食材</text><button class="ui-button secondary select-food" disabled>选择食材 · 待接入</button></view>
            <view class="field"><text class="field-label">数量</text><input v-model="draft.quantity" class="ui-input" inputmode="decimal" placeholder="留空表示未知" /></view>
            <view class="field"><text class="field-label">采购单位</text><input v-model="draft.purchaseUnit" class="ui-input" placeholder="例如 lb" /></view>
            <view class="field"><text class="field-label">单价</text><input v-model="draft.unitPrice" class="ui-input" inputmode="decimal" placeholder="留空表示未知" /></view>
            <view class="field"><text class="field-label">金额</text><input v-model="draft.lineAmount" class="ui-input" inputmode="decimal" placeholder="留空表示未知" /></view>
          </view>
          <text class="placeholder draft-note">仅供排版与焦点预览；输入不会保存，也不会生成采购记录。</text>
          <view class="desktop-actions"><button class="ui-button" disabled>确认此行</button><button class="ui-button secondary" disabled>跳过此行</button></view>
        </view>

        <view class="ui-card tail-card"><text class="pane-title">行列表与对账</text><text class="placeholder">行状态、金额和差额将在 F04–F09 接入真实数据。</text></view>
      </view>
    </view>

    <view class="mobile-action"><text class="placeholder">布局预览 · 无法提交</text><button class="ui-button" disabled>确认此行</button></view>
  </view>
</template>

<style scoped>
.review-layout { min-width: 0; display: flex; flex-direction: column; gap: 20px; }
.desktop-heading { display: flex; align-items: end; justify-content: space-between; gap: 20px; }
.back-link { min-height: 44px; margin: 0 0 4px -10px; padding: 8px 10px; border: 0; background: transparent; color: var(--green); font-size: 14px; text-align: left; cursor: pointer; }
.back-link::after { border: 0; }
.review-title { display: block; font-size: 28px; font-weight: 700; line-height: 1.25; }
.review-subtitle { display: block; margin-top: 6px; font-size: 14px; }
.preview-badge { flex: none; padding: 6px 10px; border-radius: 6px; background: #edf3eb; color: var(--ink); font-size: 13px; font-weight: 600; }
.mobile-tabs { display: none; }
.review-grid { min-width: 0; height: max(540px, calc(100vh - 270px)); display: grid; grid-template-columns: minmax(0, 45fr) minmax(0, 55fr); align-items: start; gap: 20px; }
.document-pane { min-width: 0; height: 100%; display: flex; flex-direction: column; overflow-y: auto; }
.pane-heading { display: flex; align-items: end; justify-content: space-between; gap: 12px; padding: 20px; }
.pane-title { display: block; margin-top: 6px; font-size: 17px; font-weight: 650; }
.document-canvas { min-height: 560px; display: flex; align-items: center; justify-content: center; padding: 24px; background: #eaece6; }
.paper-outline { width: min(100%, 340px); min-height: 470px; display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 14px; border: 1px dashed #a5b3a5; background: #fff; text-align: center; }
.paper-caption { color: var(--ink); font-size: 20px; font-weight: 650; }
.document-foot { padding: 15px 20px; }
.form-pane { min-width: 0; height: 100%; display: flex; flex-direction: column; gap: 16px; overflow-y: auto; }
.header-card, .row-card, .tail-card { min-width: 0; padding: 20px; }
.header-card, .tail-card { display: flex; flex-direction: column; gap: 8px; }
.row-heading { display: flex; align-items: end; justify-content: space-between; gap: 8px; }
.row-count { flex: none; color: var(--muted); font-size: 13px; }
.status-row { display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 16px 0; color: var(--muted); font-size: 12px; }
.crop-placeholder { min-height: 82px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px; border: 1px dashed #c4d1c5; border-radius: 8px; background: #f7f9f5; }
.crop-label { font-size: 13px; font-weight: 650; }
.fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-top: 20px; }
.field { min-width: 0; display: flex; flex-direction: column; gap: 8px; }
.field.full { grid-column: 1 / -1; }
.field-label { font-size: 13px; font-weight: 650; }
.select-food { width: 100%; justify-content: start; font-weight: 400; }
.draft-note { display: block; margin-top: 16px; }
.desktop-actions { display: flex; gap: 10px; margin-top: 20px; }
.mobile-action { display: none; }
@media (max-width: 899px) {
  .review-layout { gap: 12px; }
  .desktop-heading { display: none; }
  .mobile-tabs { display: flex; padding: 4px; border-radius: 9px; background: #e5ebe2; }
  .mobile-tabs button { flex: 1; min-height: 44px; margin: 0; border: 0; border-radius: 7px; background: transparent; color: var(--muted); font-size: 14px; cursor: pointer; }
  .mobile-tabs button::after { border: 0; }
  .mobile-tabs button.active { background: #fff; color: var(--ink); font-weight: 700; }
  .review-grid { display: block; height: auto; }
  .mobile-hidden { display: none; }
  .document-pane { min-height: 550px; height: auto; overflow: visible; }
  .document-canvas { min-height: 450px; padding: 16px; }
  .paper-outline { min-height: 390px; }
  .header-card, .row-card, .tail-card { padding: 16px; }
  .form-pane { height: auto; gap: 12px; overflow: visible; }
  .crop-placeholder { align-items: start; flex-direction: column; min-height: 90px; }
  .fields { gap: 14px 12px; }
  .desktop-actions { display: none; }
  .mobile-action { position: fixed; z-index: 20; left: 0; right: 0; bottom: 0; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px max(16px, env(safe-area-inset-right)) calc(12px + env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left)); border-top: 1px solid var(--line); background: #fff; }
  .mobile-action .placeholder { max-width: 150px; font-size: 12px; }
}
</style>
