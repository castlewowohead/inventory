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
  /** multipart field name. The invoice endpoints expect `files`. */
  fieldName: string
  formData?: Record<string, string>
  onProgress?: (progress: UploadProgress) => void
}

export interface UploadHandle<T> {
  response: Promise<T>
  /** Abort the in-flight request (the browser stops sending bytes). */
  abort: () => void
}

function parse(status: number, body: string): unknown {
  if (!body) return null
  try {
    return JSON.parse(body)
  } catch {
    throw new ApiError({
      code: 'invalid_response',
      message: `服务端返回了无法解析的内容（HTTP ${status}）。请检查后端日志。`
    }, status)
  }
}

/**
 * A failed request is not always a dead server: an aborted upload (the user left
 * the page) and a timeout are different events and must read differently, or the
 * page would blame the backend for something the user did.
 */
function uploadFailure(reason: unknown): ApiError {
  const message = reason && typeof reason === 'object' && 'errMsg' in reason
    ? String((reason as { errMsg?: unknown }).errMsg ?? '')
    : ''
  if (message.includes('abort')) {
    return new ApiError({
      code: 'upload_aborted',
      message: '上传已中断。已经保存成功的页仍然留在单据上，可以继续补传剩下的页。'
    })
  }
  if (message.includes('timeout')) {
    return new ApiError({
      code: 'upload_timeout',
      message: '上传超时。文件可能已经部分发送，请打开这张单据核对已有的页，再补传缺的页。'
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
    message: '上传没有收到服务端的回应。两种常见原因：文件超过服务端允许的大小（服务端会直接断开连接），'
      + '或者后端没有启动、网络中断。请确认 Flask 在 127.0.0.1:5000 运行，再压缩照片或把多页 PDF 拆开重试。',
    details: cause ? { cause } : undefined
  })
}

/** 413 comes from Flask's MAX_CONTENT_LENGTH, before any business validation runs. */
function tooLargeError(status: number, data: unknown): ApiError {
  const fallback = responseError(status, data)
  return new ApiError({
    code: fallback.code,
    message: `${fallback.message} 单个文件默认上限 20 MB，实际上限由后端 `
      + 'UPLOAD_MAX_BYTES 决定；请压缩照片或把多页 PDF 拆开再传。',
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
          body = parse(status, typeof result.data === 'string' ? result.data : '')
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
      fail: (reason) => reject(uploadFailure(reason))
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
