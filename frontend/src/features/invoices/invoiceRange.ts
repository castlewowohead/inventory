/**
 * 单据列表「选定时间范围」背后的日历日处理。
 *
 * 一律按**本地日历日**当字符串处理，不碰 `Date` 的时区转换：`2026-06-08` 这种
 * 日历日一旦被当成 UTC 时刻解析，在西半球会退回前一天，整段范围就错位一天。
 * 后端只认一对闭区间参数 `date_from` / `date_to`，这里也只产生这两个值。
 */

/** `2026-02-31` 这种不存在的日子要判成假，否则它会被原样发给服务端。 */
export function isCalendarDay(value: string): boolean {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value ?? '')
  if (!match) return false
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])]
  const at = new Date(Date.UTC(year, month - 1, day, 12))
  return at.getUTCMonth() === month - 1 && at.getUTCDate() === day
}

/** `2026-06-08` → `2026年6月8日`。不是日历日就原样返回。 */
export function dayLabel(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value ?? '')
  if (!match) return value
  return `${Number(match[1])}年${Number(match[2])}月${Number(match[3])}日`
}

/** 给用户看的那句话。两端都可以空着——空着就是这一端不限。 */
export function rangeLabel(from: string, to: string): string {
  if (from && to) return from === to ? dayLabel(from) : `${dayLabel(from)} – ${dayLabel(to)}`
  if (from) return `${dayLabel(from)} 起`
  if (to) return `截至 ${dayLabel(to)}`
  return '选定时间范围'
}

/** 列表请求上要带的查询串片段。两端都空就不筛。 */
export function rangeQuery(from: string, to: string): string {
  let query = ''
  if (isCalendarDay(from)) query += `&date_from=${from}`
  if (isCalendarDay(to)) query += `&date_to=${to}`
  return query
}
