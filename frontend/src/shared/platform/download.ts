import type { BinaryResponse } from '../api/request'

/** Save the exact attachment returned by the API; no client-side re-encoding. */
export function downloadAttachment(response: BinaryResponse, filename: string): void {
  // #ifdef H5
  const blob = new Blob([response.bytes], { type: response.mediaType || 'application/octet-stream' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
  // #endif
  // #ifndef H5
  throw new Error('App 端尚未实现表格下载，请在 Web 上导出。')
  // #endif
}
