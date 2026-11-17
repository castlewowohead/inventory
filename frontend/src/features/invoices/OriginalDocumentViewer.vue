<script setup lang="ts">
/**
 * The original document pane (F06).
 *
 * It shows the stored bytes of one uploaded file — never a re-encoded or
 * "enhanced" copy: this pane is the evidence the user checks the draft against,
 * so `GET .../files/{id}/content` returns the file byte for byte and the page
 * only scales it.
 *
 * File reading, HEIC probing and PDF rendering all live in
 * `shared/platform/documentPreview.ts`; this component holds no browser API.
 *
 * View state (file, page, zoom, scroll offset) is component state on purpose.
 * The parent swaps this pane in and out when the narrow layout toggles
 * 原件 / 填写, and a pane that forgot its page would send the user back to
 * hunting for the row they were reading.
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { errorMessage, getInvoiceFileContent, type InvoiceFile } from '../../shared/api'
import {
  classifyDocument, isHeic, openPdf, renderImage,
  PreviewUnavailableError,
  type DocumentKind, type PdfDocument, type RenderedPage
} from '../../shared/platform/documentPreview'
import { formatBytes } from '../../shared/format'

const props = defineProps<{
  invoiceId: number
  files: InvoiceFile[]
  isFake: boolean
  /** Where the selected line's evidence says to look. */
  focus: { fileId: number | null; page: number | null } | null
  /** False while the narrow layout shows the form instead. */
  visible: boolean
}>()

const ZOOM_STEPS = [1, 1.5, 2, 3, 4] as const
/** How sharp a rendered PDF page is, in device pixels across. */
const PDF_BASE_WIDTH = 1600
const PDF_MAX_WIDTH = 4000

const ordered = computed(() => [...props.files].sort((a, b) => a.page_order - b.page_order))
const fileKey = computed(() => ordered.value.map((file) => file.id).join(','))
const currentFileId = ref<number | null>(null)
const current = computed(() =>
  ordered.value.find((file) => file.id === currentFileId.value) ?? ordered.value[0] ?? null)
const kind = computed<DocumentKind>(() =>
  current.value ? classifyDocument(current.value.original_name, current.value.content_type) : 'unknown')

const page = ref(1)
const pageCount = ref(1)
const zoomIndex = ref(0)
const zoom = computed(() => ZOOM_STEPS[zoomIndex.value])
const rendered = ref<RenderedPage | null>(null)
const state = ref<'empty' | 'loading' | 'ready' | 'unavailable' | 'error'>('empty')
const failure = ref('')
/** Restore target for the scroll container; see `restoreScroll`. */
const scrollTop = ref(0)
const scrollLeft = ref(0)

const bytesCache = new Map<number, ArrayBuffer>()
let pdf: PdfDocument | null = null
let pdfFileId: number | null = null
let renderSequence = 0
let lastTop = 0
let lastLeft = 0

function releaseRendered() {
  rendered.value?.release()
  rendered.value = null
}

function closePdf() {
  pdf?.close()
  pdf = null
  pdfFileId = null
}

async function bytesFor(file: InvoiceFile): Promise<ArrayBuffer> {
  const cached = bytesCache.get(file.id)
  if (cached) return cached
  const response = await getInvoiceFileContent(props.invoiceId, file.id)
  bytesCache.set(file.id, response.bytes)
  return response.bytes
}

async function render() {
  const sequence = (renderSequence += 1)
  const file = current.value
  releaseRendered()
  if (!file) {
    state.value = 'empty'
    return
  }
  state.value = 'loading'
  failure.value = ''
  try {
    if (kind.value === 'unknown') {
      state.value = 'unavailable'
      failure.value = `${file.original_name} 不是图片或 PDF（${file.content_type || '媒体类型未记录'}），`
        + '这里不做预览。原件已完整保存，可以下载后用对应的程序打开。'
      return
    }
    const bytes = await bytesFor(file)
    if (sequence !== renderSequence) return
    if (kind.value === 'pdf') {
      if (pdfFileId !== file.id) {
        closePdf()
        pdf = await openPdf(bytes)
        pdfFileId = file.id
        if (sequence !== renderSequence) return
      }
      pageCount.value = pdf?.pageCount ?? 1
      if (page.value > pageCount.value) page.value = 1
      const target = Math.min(PDF_MAX_WIDTH, Math.round(PDF_BASE_WIDTH * Math.min(zoom.value, 2.5)))
      const result = await pdf!.renderPage(page.value, target)
      if (sequence !== renderSequence) {
        result.release()
        return
      }
      rendered.value = result
    } else {
      pageCount.value = 1
      page.value = 1
      const result = await renderImage(bytes, current.value.content_type, file.original_name)
      if (sequence !== renderSequence) {
        result.release()
        return
      }
      rendered.value = result
    }
    state.value = 'ready'
  } catch (error) {
    if (sequence !== renderSequence) return
    releaseRendered()
    if (error instanceof PreviewUnavailableError) {
      state.value = 'unavailable'
      failure.value = error.message
    } else {
      state.value = 'error'
      failure.value = errorMessage(error)
    }
  }
}

function rememberScroll(event: { detail: { scrollTop: number; scrollLeft: number } }) {
  lastTop = event.detail.scrollTop
  lastLeft = event.detail.scrollLeft
}

/**
 * Puts the scroll container back where the user left it.
 *
 * ⚠️ The nudge through 0 is needed, not decorative: a hidden pane's DOM scroll
 * offset is reset to 0 by the browser, while the bound prop still holds the old
 * value — so assigning that same value again changes nothing and the user lands
 * at the top of the page they were reading.
 */
async function restoreScroll() {
  if (lastTop === 0 && lastLeft === 0) return
  scrollTop.value = 0
  scrollLeft.value = 0
  await new Promise((resolve) => setTimeout(resolve, 0))
  scrollTop.value = lastTop
  scrollLeft.value = lastLeft
}

function resetScroll() {
  lastTop = 0
  lastLeft = 0
  scrollTop.value = 0
  scrollLeft.value = 0
}

function selectFile(fileId: number) {
  if (currentFileId.value === fileId) return
  currentFileId.value = fileId
  page.value = 1
  resetScroll()
  void render()
}

function changePage(delta: number) {
  const next = page.value + delta
  if (next < 1 || next > pageCount.value) return
  page.value = next
  resetScroll()
  void render()
}

function changeZoom(delta: number) {
  const next = zoomIndex.value + delta
  if (next < 0 || next >= ZOOM_STEPS.length) return
  zoomIndex.value = next
  // An image is scaled by CSS, so only a PDF needs re-rendering to stay sharp.
  if (kind.value === 'pdf') void render()
}

// A re-read of the invoice hands over a new array with the same files; only an
// actual change of the file set may touch the selection.
watch(fileKey, () => {
  if (!ordered.value.some((file) => file.id === currentFileId.value)) {
    currentFileId.value = ordered.value[0]?.id ?? null
    page.value = 1
    resetScroll()
  }
  void render()
}, { immediate: true })

// Selecting a line jumps to the page its evidence points at. A line without a
// recorded file or page leaves the pane where it is — there is nothing to jump to.
watch(() => props.focus, (focus) => {
  if (!focus) return
  let moved = false
  if (focus.fileId !== null && ordered.value.some((file) => file.id === focus.fileId)
    && focus.fileId !== currentFileId.value) {
    currentFileId.value = focus.fileId
    moved = true
  }
  if (focus.page !== null && focus.page >= 1 && focus.page !== page.value) {
    page.value = focus.page
    moved = true
  }
  if (moved) {
    resetScroll()
    void render()
  }
}, { deep: true })

watch(() => props.visible, (visible) => { if (visible) void restoreScroll() })

onUnmounted(() => {
  renderSequence += 1
  releaseRendered()
  closePdf()
  bytesCache.clear()
})

defineExpose({ reload: render })
</script>

<template>
  <view class="viewer">
    <view class="viewer-head">
      <view class="head-text">
        <text class="eyebrow">ORIGINAL / 原件</text>
        <text class="viewer-title">{{ current ? current.original_name : '这张单据还没有原件' }}</text>
        <text v-if="current" class="muted meta">
          第 {{ current.page_order }} 个文件 · {{ formatBytes(current.size) }}
          · {{ current.content_type || '媒体类型未记录' }}
          <template v-if="kind === 'pdf'"> · PDF 共 {{ pageCount }} 页</template>
        </text>
      </view>
      <text v-if="isFake" class="demo-tag">演示识别结果</text>
    </view>

    <view v-if="ordered.length > 1" class="file-tabs" aria-label="选择原件">
      <button v-for="file in ordered" :key="file.id" class="file-tab"
        :class="{ active: current?.id === file.id }"
        :aria-current="current?.id === file.id ? 'true' : undefined"
        @click="selectFile(file.id)">
        第 {{ file.page_order }} 页 · {{ file.original_name }}
      </button>
    </view>

    <view class="toolbar">
      <view v-if="kind === 'pdf'" class="toolbar-group">
        <button class="ui-button secondary small" :disabled="page <= 1" @click="changePage(-1)">上一页</button>
        <text class="toolbar-text">{{ page }} / {{ pageCount }} 页</text>
        <button class="ui-button secondary small" :disabled="page >= pageCount" @click="changePage(1)">下一页</button>
      </view>
      <view class="toolbar-group">
        <button class="ui-button secondary small" :disabled="zoomIndex === 0" @click="changeZoom(-1)">缩小</button>
        <text class="toolbar-text">{{ zoom === 1 ? '适应宽度' : `${zoom}×` }}</text>
        <button class="ui-button secondary small" :disabled="zoomIndex >= ZOOM_STEPS.length - 1" @click="changeZoom(1)">放大</button>
      </view>
    </view>

    <scroll-view class="canvas" scroll-x scroll-y :scroll-top="scrollTop" :scroll-left="scrollLeft"
      :scroll-with-animation="false" @scroll="rememberScroll">
      <view v-if="state === 'loading'" class="canvas-state" role="status" aria-live="polite">正在读取原件…</view>
      <view v-else-if="state === 'empty'" class="canvas-state">这张单据上没有文件，没有原件可看。</view>
      <view v-else-if="state === 'unavailable'" class="canvas-state">
        <text class="unavailable-title">这个文件不能在页面里预览</text>
        <text class="placeholder">{{ failure }}</text>
        <text v-if="current && isHeic(current.original_name, current.content_type)" class="placeholder">
          这是浏览器的解码能力问题，不是上传失败：服务端那份字节没有改动过。
        </text>
      </view>
      <view v-else-if="state === 'error'" class="canvas-state" role="alert">
        <text class="unavailable-title">原件读取失败</text>
        <text class="placeholder">{{ failure }}</text>
        <button class="ui-button secondary" @click="render">重试读取</button>
      </view>
      <image v-else-if="rendered" class="paper" :src="rendered.url" mode="widthFix"
        :style="{ width: `${zoom * 100}%` }"
        :aria-label="`${current?.original_name ?? '原件'} 第 ${page} 页`" />
    </scroll-view>
  </view>
</template>

<style scoped>
.viewer { min-width: 0; height: 100%; display: flex; flex-direction: column; }
.viewer-head { display: flex; align-items: start; justify-content: space-between; gap: 12px; padding: 18px 20px 12px; }
.head-text { min-width: 0; }
.viewer-title { display: block; margin-top: 6px; font-size: 17px; font-weight: 650; overflow-wrap: anywhere; }
.meta { display: block; margin-top: 4px; font-size: 12px; }
.demo-tag { flex: none; padding: 4px 8px; border-radius: 5px; background: #fff5e9; color: #704007; font-size: 12px; font-weight: 700; }
.file-tabs { display: flex; flex-wrap: wrap; gap: 8px; padding: 0 20px 12px; }
.file-tab { max-width: 100%; min-height: 44px; margin: 0; padding: 7px 10px; border: 1px solid var(--line); border-radius: 7px; background: #fff; color: var(--muted); font-size: 12px; text-align: left; cursor: pointer; overflow-wrap: anywhere; }
.file-tab::after { border: 0; }
.file-tab.active { border-color: var(--green); background: #eef4ec; color: var(--ink); font-weight: 700; }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 20px; padding: 0 20px 12px; }
.toolbar-group { display: flex; align-items: center; gap: 8px; }
.toolbar-text { color: var(--muted); font-size: 13px; min-width: 72px; text-align: center; }
/* Still 44px tall: the toolbar lives next to the original on a phone, where the
   plan sets 44×44 as the tap-target goal. Only the type and padding shrink. */
.ui-button.small { min-height: 44px; min-width: 64px; padding: 6px 10px; font-size: 13px; font-weight: 500; }
.canvas { flex: 1; min-height: 320px; background: #eaece6; border-top: 1px solid var(--line); }
.canvas-state { min-height: 320px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; padding: 28px; text-align: center; }
.unavailable-title { font-size: 15px; font-weight: 650; }
.paper { display: block; }
@media (max-width: 899px) {
  .viewer-head { padding: 14px 16px 10px; }
  .file-tabs, .toolbar { padding-left: 16px; padding-right: 16px; }
  .canvas { min-height: 420px; }
}
</style>
