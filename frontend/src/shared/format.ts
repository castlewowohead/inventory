/** Display helpers shared by the feature components. */

/** Human-readable file size. Keeps the exact byte count available separately. */
export function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size < 0) return '大小未知'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(size < 10 * 1024 ? 1 : 0)} KB`
  return `${(size / (1024 * 1024)).toFixed(size < 10 * 1024 * 1024 ? 1 : 0)} MB`
}

/** `2026-11-11T08:12:30+00:00` → `11-11 08:12`. Empty input stays empty. */
export function formatClock(value: string | null): string {
  if (!value) return ''
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(value)
  if (!match) return value
  return `${match[2]}-${match[3]} ${match[4]}:${match[5]}`
}
