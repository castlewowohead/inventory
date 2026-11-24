import { networkError, responseError } from './errors'

type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

export interface RequestOptions {
  method?: Method
  data?: Record<string, unknown>
  headers?: Record<string, string>
  timeout?: number
}

/** Same-origin paths are proxied by Vite during H5 development. */
export function apiRequest<T>(path: `/${string}`, options: RequestOptions = {}): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    uni.request({
      url: path,
      // uni.request supports PATCH on H5; the bundled type omits it.
      method: (options.method ?? 'GET') as UniApp.RequestOptions['method'],
      data: options.data,
      header: options.headers,
      timeout: options.timeout,
      success: (response) => {
        if (response.statusCode >= 200 && response.statusCode < 300) {
          resolve(response.data as T)
        } else {
          reject(responseError(response.statusCode, response.data))
        }
      },
      fail: (reason) => reject(networkError(reason))
    })
  })
}

export interface BinaryResponse {
  bytes: ArrayBuffer
  /** The `Content-Type` the server sent, without parameters. May be empty. */
  mediaType: string
  /** Lower-cased response header names → values. */
  headers: Record<string, string>
}

function lowerCaseHeaders(raw: unknown): Record<string, string> {
  const headers: Record<string, string> = {}
  if (raw && typeof raw === 'object') {
    for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
      headers[key.toLowerCase()] = String(value)
    }
  }
  return headers
}

/**
 * An error body that arrived as bytes because the request asked for bytes.
 *
 * The crop and original-file endpoints answer with an image on success and with
 * the usual `{"error": {...}}` JSON on failure, so a binary request still has to
 * be able to read the backend's own wording — a 404 from the crop endpoint
 * carries the reason the cell has no coordinates, which the review screen shows
 * instead of a generic failure.
 */
function decodeErrorBody(bytes: ArrayBuffer): unknown {
  if (typeof TextDecoder === 'undefined') return null
  try {
    return JSON.parse(new TextDecoder('utf-8').decode(bytes))
  } catch {
    return null
  }
}

/**
 * Request raw bytes (original files, field crops, selected-invoice exports).
 *
 * Response headers are handed back because the crop endpoint puts its evidence
 * there: `X-Field`, `X-Page`, `X-Model-Values` and `X-Agreement`.
 */
export function apiRequestBinary(path: `/${string}`, options: RequestOptions = {}): Promise<BinaryResponse> {
  return new Promise<BinaryResponse>((resolve, reject) => {
    uni.request({
      url: path,
      method: (options.method ?? 'GET') as UniApp.RequestOptions['method'],
      data: options.data,
      header: options.headers,
      timeout: options.timeout,
      responseType: 'arraybuffer',
      success: (response) => {
        const headers = lowerCaseHeaders(response.header)
        const bytes = response.data instanceof ArrayBuffer ? response.data : new ArrayBuffer(0)
        if (response.statusCode >= 200 && response.statusCode < 300) {
          resolve({
            bytes,
            mediaType: (headers['content-type'] ?? '').split(';')[0].trim().toLowerCase(),
            headers
          })
        } else {
          reject(responseError(response.statusCode, decodeErrorBody(bytes)))
        }
      },
      fail: (reason) => reject(networkError(reason))
    })
  })
}
