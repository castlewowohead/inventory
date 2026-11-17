/**
 * Upload orchestration for one invoice (plan section 4.1).
 *
 * The order of requests is a product decision, not a convenience:
 *
 * 1. the **first** file goes to `POST /api/invoices` with `recognize=false`,
 * 2. every other file goes to `POST /api/invoices/{id}/files`,
 * 3. only after **all** pages are stored does a non-manual upload call
 *    `POST /api/invoices/{id}/extraction`.
 *
 * Uploading with `recognize=true` would let the backend start recognition on
 * page 1 while page 2 is still being sent - the model would read one page of a
 * two-page invoice and nothing would say so. A failure half way keeps the
 * invoice and the pages already stored, so the user can finish from the detail
 * view instead of starting over.
 */
import { ref, computed } from 'vue'
import { ApiError, errorMessage, startExtraction, type Invoice } from '../../shared/api'
import { uploadOne } from '../../shared/platform/upload'
import type { PickedFile } from '../../shared/platform/filePicker'
import { formatBytes } from '../../shared/format'

/** Mirrors backend ALLOWED_UPLOAD_EXTENSIONS; the server still has the last word. */
export const ALLOWED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'pdf', 'heic'] as const
/** Mirrors backend MAX_FILES_PER_INVOICE. */
export const MAX_FILES_PER_INVOICE = 20
/** Mirrors the default UPLOAD_MAX_BYTES (20 MB). A lower server limit still rejects. */
export const DEFAULT_MAX_BYTES = 20 * 1024 * 1024

export type ItemState = 'ready' | 'invalid' | 'uploading' | 'done' | 'failed'

export interface QueueItem {
  key: string
  file: PickedFile
  state: ItemState
  /** 0-100 while uploading: bytes actually sent, never a guess. */
  progress: number
  message: string
}

export function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot < 0 ? '' : name.slice(dot + 1).toLowerCase()
}

/** Local pre-check so obvious problems are named before a request goes out. */
export function checkFile(file: PickedFile): string | null {
  const extension = extensionOf(file.name)
  if (!extension) {
    return '这个文件没有扩展名，无法判断类型。请选择 JPG、PNG、PDF 或 HEIC 文件。'
  }
  if (!(ALLOWED_EXTENSIONS as readonly string[]).includes(extension)) {
    return `不支持 .${extension}；可以上传 ${ALLOWED_EXTENSIONS.join(' / ')}。`
  }
  if (file.size === 0) return '这个文件是空的（0 字节）。'
  if (file.size > DEFAULT_MAX_BYTES) {
    return `${formatBytes(file.size)} 超过单个文件 20 MB 的默认上限，请压缩或分页后再传。`
  }
  return null
}

let sequence = 0

export interface RunTarget {
  /** `null` creates a new invoice with the first file; a number appends to it. */
  invoiceId: number | null
  /** Non-manual uploads start extraction once every page is stored. */
  startExtractionWhenDone: boolean
}

export interface RunOutcome {
  invoiceId: number | null
  existingInvoiceId?: number
  uploaded: number
  failed: boolean
  extraction: 'skipped' | 'started' | 'failed'
  message: string
}

export function useUploadQueue() {
  const items = ref<QueueItem[]>([])
  const running = ref(false)
  const activeIndex = ref(-1)
  const invoiceId = ref<number | null>(null)
  let abortActive: (() => void) | null = null

  const hasInvalid = computed(() => items.value.some((item) => item.state === 'invalid'))
  const hasHeic = computed(() => items.value.some((item) => extensionOf(item.file.name) === 'heic'))
  const doneCount = computed(() => items.value.filter((item) => item.state === 'done').length)
  const remainingCount = computed(() => items.value.filter((item) => item.state !== 'done').length)

  function reset() {
    items.value = []
    invoiceId.value = null
    activeIndex.value = -1
    running.value = false
  }

  /** Add picked files. `existing` is how many files the invoice already holds. */
  function add(files: PickedFile[], existing = 0): string | null {
    let overflow = 0
    for (const file of files) {
      if (existing + items.value.length >= MAX_FILES_PER_INVOICE) {
        overflow += 1
        continue
      }
      const problem = checkFile(file)
      items.value.push({
        key: `f${(sequence += 1)}`,
        file,
        state: problem ? 'invalid' : 'ready',
        progress: 0,
        message: problem ?? ''
      })
    }
    if (overflow > 0) {
      return `一张单据最多 ${MAX_FILES_PER_INVOICE} 个文件，有 ${overflow} 个没有加入。`
    }
    return null
  }

  function remove(key: string) {
    if (running.value) return
    items.value = items.value.filter((item) => item.key !== key)
  }

  /** Page order is the list order, so the user can fix it before uploading. */
  function move(key: string, delta: number) {
    if (running.value) return
    const index = items.value.findIndex((item) => item.key === key)
    const next = index + delta
    if (index < 0 || next < 0 || next >= items.value.length) return
    const copy = items.value.slice()
    const [moved] = copy.splice(index, 1)
    copy.splice(next, 0, moved)
    items.value = copy
  }

  function cancel() {
    abortActive?.()
  }

  /**
   * Upload every item that is not stored yet, in list order. Stops at the first
   * failure; the pages already stored stay on the invoice, and a second call
   * continues with the remaining ones.
   */
  async function run(target: RunTarget): Promise<RunOutcome> {
    const idle: RunOutcome = {
      invoiceId: invoiceId.value, uploaded: 0, failed: true,
      extraction: 'skipped', message: ''
    }
    if (running.value) return { ...idle, message: '上传正在进行中。' }
    if (hasInvalid.value) return { ...idle, message: '请先移除不支持的文件，再开始上传。' }
    if (!items.value.length) return { ...idle, message: '还没有选择文件。' }

    running.value = true
    if (invoiceId.value === null) invoiceId.value = target.invoiceId
    let uploaded = 0
    let failure = ''
    let existingInvoiceId: number | undefined
    try {
      for (let index = 0; index < items.value.length; index += 1) {
        const item = items.value[index]
        if (item.state === 'done') continue
        activeIndex.value = index
        item.state = 'uploading'
        item.progress = 0
        item.message = ''
        const createsInvoice = invoiceId.value === null
        const path = createsInvoice ? '/api/invoices' : `/api/invoices/${invoiceId.value}/files`
        const handle = uploadOne<Invoice>(path as `/${string}`, item.file, {
          fieldName: 'files',
          // `recognize=false` on create: recognition waits for every page.
          formData: createsInvoice ? { recognize: 'false' } : {},
          onProgress: (progress) => { item.progress = progress.progress }
        })
        abortActive = handle.abort
        try {
          const invoice = await handle.response
          invoiceId.value = invoice.id
          item.state = 'done'
          item.progress = 100
          uploaded += 1
        } catch (error) {
          const found = error instanceof ApiError ? error.details?.invoice_id : undefined
          if (error instanceof ApiError && error.status === 409
            && typeof error.details?.sha256 === 'string'
            && Number.isSafeInteger(error.details?.invoice_file_id)
            && Number.isSafeInteger(found) && (found as number) > 0) {
            existingInvoiceId = found as number
          }
          item.state = 'failed'
          item.message = errorMessage(error)
          failure = item.message
          break
        } finally {
          abortActive = null
        }
      }
    } finally {
      activeIndex.value = -1
      running.value = false
    }

    if (failure) {
      return {
        invoiceId: invoiceId.value, uploaded, failed: true,
        existingInvoiceId,
        extraction: 'skipped', message: failure
      }
    }
    if (!target.startExtractionWhenDone || invoiceId.value === null) {
      return {
        invoiceId: invoiceId.value, uploaded, failed: false,
        extraction: 'skipped', message: ''
      }
    }
    try {
      await startExtraction(invoiceId.value)
      return {
        invoiceId: invoiceId.value, uploaded, failed: false,
        extraction: 'started', message: ''
      }
    } catch (error) {
      // The pages are stored; only the recognition request failed. Say exactly that.
      return {
        invoiceId: invoiceId.value,
        uploaded,
        failed: false,
        extraction: 'failed',
        message: `原件已全部保存，但启动识别失败：${errorMessage(error)}`
      }
    }
  }

  return {
    items, running, activeIndex, invoiceId,
    hasInvalid, hasHeic, doneCount, remainingCount,
    add, remove, move, run, cancel, reset
  }
}
