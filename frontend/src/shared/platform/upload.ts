/**
 * Multipart upload, isolated from the invoice components.
 *
 * `uni.uploadFile` is one request per file on purpose (plan §4.1): one file per
 * request avoids the multipart differences between platforms, and a failed page
 * only loses that page. Progress comes from the real XHR upload event — the
 * number is bytes actually sent, not a made-up percentage.
 */
import { ApiError, networkError, responseError } from '../api/errors'
import type { PickedFile } from './filePicker'

export interface UploadProgress {
  /** 0–100, from `XMLHttpRequest.upload.onprogress`. */
  progress: number
  sent: number
  total: number
}

export interface UploadOptions {
  /** Multipart field name: legacy invoices use `files`, intake uses `file`. */
  fieldName: string
  context?: 'invoice' | 'batch'
  formData?: Record<string, string>
  onProgress?: (progress: UploadProgress) => void
}

export interface UploadHandle<T> {
  response: Promise<T>
  /** Abort the in-flight request (the browser stops sending bytes). */
  abort: () => void
}

function parse(status: number, body: string, context: 'invoice' | 'batch'): unknown {
  if (!body) return null
  try {
    return JSON.parse(body)
  } catch {
    throw new ApiError({
      code: 'invalid_response',
      message: context === 'batch' ? '暂时无法确认上传结果，请返回整理批次查看文件是否已保存，再重试。' : '暂时无法确认上传结果，请返回单据查看文件是否已保存，再重试。'
    }, status)
  }
}

/**
 * A failed request is not always a dead server: an aborted upload (the user left
 * the page) and a timeout are different events and must read differently, or the
 * page would blame the backend for something the user did.
 */
function uploadFailure(reason: unknown, context: 'invoice' | 'batch'): ApiError {
  const destination = context === 'batch' ? '整理批次' : '单据'
  const message = reason && typeof reason === 'object' && 'errMsg' in reason
    ? String((reason as { errMsg?: unknown }).errMsg ?? '')
    : ''
  if (message.includes('abort')) {
    return new ApiError({
      code: 'upload_aborted',
      message: `上传已中断。已经保存成功的文件仍留在${destination}中，可以继续上传。`
    })
  }
  if (message.includes('timeout')) {
    return new ApiError({
      code: 'upload_timeout',
      message: `上传超时。文件可能已经部分发送，请打开${destination}核对已保存的文件，再上传缺少的文件。`
    })
  }
  // ⚠️ An oversized upload usually arrives here, not in the 413 branch: the
  // server answers 413 and closes the connection while the browser is still
  // sending, so the browser reports a transport error and the response is lost
  // (verified against the local Flask dev server). Naming only "backend is
  // down" would send the user looking in the wrong place, so both causes are
  // stated. A silent "ok" is never an option.
  const cause = networkError(reason).details?.cause
  return new ApiError({
    code: 'upload_failed',
    message: '上传没有完成。请检查网络；如果文件较大，可压缩照片或把多页 PDF 拆开后重试。'
      + `重试前请先查看${destination}，确认文件是否已经保存。`,
    details: cause ? { cause } : undefined
  })
}

/** 413 comes from Flask's MAX_CONTENT_LENGTH, before any business validation runs. */
function tooLargeError(status: number, data: unknown): ApiError {
  const fallback = responseError(status, data)
  return new ApiError({
    code: fallback.code,
    message: '文件太大，无法上传。请压缩照片或把多页 PDF 拆开后重试。',
    details: fallback.details
  }, status)
}

export function uploadOne<T>(path: `/${string}`, file: PickedFile, options: UploadOptions): UploadHandle<T> {
  let abort = () => {}
  const response = new Promise<T>((resolve, reject) => {
    // ⚠️ A `File` must be passed through `files`, not through `file`:
    // uni-h5 3.0.0-alpha-5030620261106001 runs `if (!filePath) reject('file error')`
    // whenever `files` is not an array — it then uploads anyway, so the request
    // reaches the server while the caller is told it failed. The array form skips
    // that branch and keeps the original filename, which the backend validates.
    const isPath = typeof file.source === 'string'
    const task = uni.uploadFile({
      url: path,
      name: options.fieldName,
      // H5 hands back a real `File`; other platforms upload from a local path.
      files: isPath ? undefined : [{ name: options.fieldName, file: file.source as File }],
      filePath: isPath ? (file.source as string) : undefined,
      formData: options.formData ?? {},
      success: (result) => {
        const status = result.statusCode
        let body: unknown
        try {
          body = parse(status, typeof result.data === 'string' ? result.data : '', options.context ?? 'invoice')
        } catch (error) {
          reject(error)
          return
        }
        if (status >= 200 && status < 300) {
          resolve(body as T)
        } else if (status === 413) {
          reject(tooLargeError(status, body))
        } else {
          reject(responseError(status, body))
        }
      },
      fail: (reason) => reject(uploadFailure(reason, options.context ?? 'invoice'))
    })
    if (options.onProgress) {
      const onProgress = options.onProgress
      task.onProgressUpdate((event) => onProgress({
        progress: event.progress,
        sent: event.totalBytesSent,
        total: event.totalBytesExpectedToSend
      }))
    }
    abort = () => task.abort()
  })
  return { response, abort: () => abort() }
}
