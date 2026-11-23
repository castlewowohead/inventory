/**
 * File selection, isolated from the invoice components.
 *
 * Why an adapter: `uni.chooseFile` is **not supported on App** (uni-app
 * compatibility table), while `uni.chooseImage` is. Keeping both calls here means
 * the App adapter task has one file to change, and business code never branches
 * on the platform.
 *
 * On H5 both APIs create a hidden `<input type="file">` and click it, so a picker
 * must be called straight from a user gesture — never after an `await`.
 */

/** A file the user selected. `source` is what the upload adapter needs. */
export interface PickedFile {
  name: string
  size: number
  contentType: string
  /** H5 hands back a real `File`; other platforms hand back a local path. */
  source: File | string
}

export class PickerUnavailableError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'PickerUnavailableError'
  }
}

export class PickerCancelledError extends Error {
  constructor() {
    super('已取消选择。')
    this.name = 'PickerCancelledError'
  }
}

/** Extensions the backend accepts (src/services/invoices.py ALLOWED_UPLOAD_EXTENSIONS). */
export const DOCUMENT_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.pdf', '.heic'] as const
export const INTAKE_EXTENSIONS = [...DOCUMENT_EXTENSIONS, '.csv', '.xlsx'] as const

function isCancel(reason: unknown): boolean {
  const message = reason && typeof reason === 'object' && 'errMsg' in reason
    ? String((reason as { errMsg?: unknown }).errMsg ?? '')
    : String(reason ?? '')
  return message.includes('cancel')
}

function normalize(entry: unknown, fallbackIndex: number): PickedFile | null {
  if (typeof File !== 'undefined' && entry instanceof File) {
    return {
      name: entry.name || `file-${fallbackIndex + 1}`,
      size: entry.size,
      contentType: entry.type || '',
      source: entry
    }
  }
  if (entry && typeof entry === 'object') {
    const item = entry as { path?: unknown; size?: unknown; name?: unknown; type?: unknown }
    if (typeof item.path === 'string') {
      const path = item.path
      const guessed = path.split(/[\/]/).pop() || `file-${fallbackIndex + 1}`
      return {
        name: typeof item.name === 'string' && item.name ? item.name : guessed,
        size: typeof item.size === 'number' ? item.size : 0,
        contentType: typeof item.type === 'string' ? item.type : '',
        source: path
      }
    }
  }
  if (typeof entry === 'string') {
    return {
      name: entry.split(/[\/]/).pop() || `file-${fallbackIndex + 1}`,
      size: 0,
      contentType: '',
      source: entry
    }
  }
  return null
}

function toPickedFiles(result: { tempFiles?: unknown; tempFilePaths?: unknown }): PickedFile[] {
  const raw = result.tempFiles ?? result.tempFilePaths
  const list = Array.isArray(raw) ? raw : raw === undefined || raw === null ? [] : [raw]
  const files: PickedFile[] = []
  list.forEach((entry, index) => {
    const picked = normalize(entry, index)
    if (picked) files.push(picked)
  })
  return files
}

/**
 * Camera or gallery photos. Supported on H5 and App.
 * `sourceType: ['camera']` makes the mobile browser open the camera directly.
 */
export function pickPhotos(from: 'camera' | 'album', count: number): Promise<PickedFile[]> {
  return new Promise((resolve, reject) => {
    uni.chooseImage({
      count: Math.max(1, count),
      sourceType: [from],
      success: (result) => resolve(toPickedFiles(result as { tempFiles?: unknown })),
      fail: (reason) => reject(isCancel(reason) ? new PickerCancelledError() : reason)
    })
  })
}

/**
 * PDF or image files from the file system.
 *
 * ⚠️ `uni.chooseFile` is **unavailable on App**; the error says so instead of
 * failing silently, and the App file picker stays an open adapter task.
 */
export function pickDocuments(count: number): Promise<PickedFile[]> {
  // #ifndef H5
  return Promise.reject(
    new PickerUnavailableError(
      '当前设备暂时无法选择 PDF 或图片文件。可以先用“拍照”上传。'
    )
  )
  // #endif
  return new Promise((resolve, reject) => {
    uni.chooseFile({
      count: Math.max(1, count),
      type: 'all',
      extension: [...DOCUMENT_EXTENSIONS],
      success: (result) => resolve(toPickedFiles(result as { tempFiles?: unknown })),
      fail: (reason) => reject(isCancel(reason) ? new PickerCancelledError() : reason)
    })
  })
}

/** Mixed intake files; each selected file still uses its own backend endpoint. */
export function pickIntakeFiles(count = 50): Promise<PickedFile[]> {
  // #ifndef H5
  return Promise.reject(new PickerUnavailableError('当前设备暂时无法选择混合文件，请在浏览器中上传。'))
  // #endif
  return new Promise((resolve, reject) => {
    uni.chooseFile({
      count: Math.max(1, count), type: 'all', extension: [...INTAKE_EXTENSIONS],
      success: (result) => resolve(toPickedFiles(result as { tempFiles?: unknown })),
      fail: (reason) => reject(isCancel(reason) ? new PickerCancelledError() : reason)
    })
  })
}

/** CSV/XLSX selection. H5 only until an App file picker is implemented. */
export function pickSpreadsheet(): Promise<PickedFile[]> {
  // #ifndef H5
  return Promise.reject(new PickerUnavailableError('当前设备暂时无法选择表格文件，请在浏览器中导入。'))
  // #endif
  return new Promise((resolve, reject) => {
    uni.chooseFile({
      count: 1,
      type: 'all',
      extension: ['.csv', '.xlsx'],
      success: (result) => resolve(toPickedFiles(result as { tempFiles?: unknown })),
      fail: (reason) => reject(isCancel(reason) ? new PickerCancelledError() : reason)
    })
  })
}

export function isCancelledPick(reason: unknown): boolean {
  return reason instanceof PickerCancelledError
}
