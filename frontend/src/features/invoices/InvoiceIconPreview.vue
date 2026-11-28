<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { errorMessage, getIntakeItemOriginal, getInvoiceFileContent, type Invoice } from '../../shared/api'
import { cropReadablePreview, openPdf, renderImage, type RenderedPage } from '../../shared/platform/documentPreview'
import { invoiceIconKind, invoiceIconLabel } from './invoiceIcon'

const props = defineProps<{ invoice: Invoice }>()
const host = ref<unknown>(null)
const imageUrl = ref('')
const state = ref<'waiting' | 'loading' | 'ready' | 'unavailable'>('waiting')
const failure = ref('')
const source = computed(() => props.invoice.intake_sources[0] ?? null)
const file = computed(() => props.invoice.files[0] ?? null)
const name = computed(() => source.value?.original_name ?? file.value?.original_name ?? '')
const kind = computed(() => invoiceIconKind(props.invoice))
const typeLabel = computed(() => invoiceIconLabel(props.invoice))

/** Spreadsheet lines keep the source cells in raw_fields; edits to draft values do not alter them. */
const tableRows = computed(() => props.invoice.lines.slice(0, 7).map((line) => {
  const cells = line.raw_fields?.cells
  if (!cells || typeof cells !== 'object') return []
  return Object.entries(cells as Record<string, { raw_text?: string }>)
    .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }))
    .slice(0, 3).map(([, cell]) => String(cell?.raw_text ?? ''))
}).filter((row) => row.length))

let rendered: RenderedPage | null = null
let observer: IntersectionObserver | null = null
let alive = true

async function load() {
  if (state.value !== 'waiting') return
  state.value = 'loading'
  if (kind.value === 'table') {
    state.value = tableRows.value.length ? 'ready' : 'unavailable'
    return
  }
  if (kind.value === 'other' || (!source.value && !file.value)) {
    state.value = 'unavailable'
    return
  }
  try {
    const response = source.value
      ? await getIntakeItemOriginal(source.value.batch_id, source.value.item_id)
      : await getInvoiceFileContent(props.invoice.id, file.value!.id)
    if (!alive) return
    if (kind.value === 'pdf') {
      const pdf = await openPdf(response.bytes)
      try {
        const page = Math.min(Math.max(source.value?.page_no ?? 1, 1), pdf.pageCount)
        const pageImage = await pdf.renderPage(page, 1100)
        try { rendered = await cropReadablePreview(pageImage) }
        finally { pageImage.release() }
      } finally { pdf.close() }
    } else {
      const pageImage = await renderImage(response.bytes, response.mediaType, name.value)
      try { rendered = await cropReadablePreview(pageImage) }
      finally { pageImage.release() }
    }
    if (!alive) { rendered?.release(); rendered = null; return }
    imageUrl.value = rendered.url
    state.value = 'ready'
  } catch (error) {
    if (alive) { failure.value = errorMessage(error); state.value = 'unavailable' }
  }
}

onMounted(() => {
  // #ifdef H5
  // uni-app may expose a view as its component proxy rather than the DOM node.
  const element = host.value instanceof Element ? host.value
    : (host.value as { $el?: Element } | null)?.$el
  if (typeof IntersectionObserver !== 'undefined' && element) {
    observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) { observer?.disconnect(); void load() }
    }, { rootMargin: '180px' })
    observer.observe(element)
    return
  }
  // #endif
  void load()
})
onUnmounted(() => { alive = false; observer?.disconnect(); rendered?.release() })
</script>

<template>
  <view ref="host" class="document" :aria-label="`${typeLabel} 原件缩略图`">
    <view class="document-top"><text class="document-type">{{ typeLabel }}</text></view>
    <view class="document-window">
      <image v-if="state === 'ready' && imageUrl" class="document-image" :src="imageUrl" mode="aspectFill" :alt="`${name} 原件放大预览`" />
      <view v-else-if="state === 'ready' && tableRows.length" class="sheet-preview" aria-hidden="true">
        <view v-for="(row, index) in tableRows" :key="index" class="sheet-row"><text v-for="(cell, column) in row" :key="column" class="sheet-cell">{{ cell }}</text></view>
      </view>
      <text v-else class="preview-fallback">{{ state === 'loading' ? '正在读取原件…' : state === 'waiting' ? '等待预览…' : failure || '暂无可显示的原件预览' }}</text>
    </view>
  </view>
</template>

<style scoped>
.document { height: 324px; overflow: hidden; box-sizing: border-box; }
.document-top { height: 30px; display: flex; align-items: center; justify-content: space-between; color: inherit; font-size: 12px; font-weight: 750; letter-spacing: .06em; }
.document-window { height: calc(100% - 30px); display: flex; align-items: center; justify-content: center; overflow: hidden; border-radius: 5px; background: #fff; }
.document-image { display: block; width: 100%; height: 100%; border: 0; border-radius: 0; background: #fff; }
.preview-fallback { padding: 14px; color: #718079; font-size: 12px; text-align: center; }
.sheet-preview { align-self: stretch; width: 100%; overflow: hidden; padding: 8px; background: repeating-linear-gradient(#fff, #fff 35px, #e5eee8 36px); }
.sheet-row { display: flex; height: 34px; border-bottom: 1px solid #dce9df; }
.sheet-cell { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: clip; padding: 7px 5px; border-right: 1px solid #dce9df; color: #3d5449; font-size: 11px; }
</style>
