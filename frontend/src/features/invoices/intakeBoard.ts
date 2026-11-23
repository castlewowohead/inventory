/**
 * Board model and card wording for the intake organizing page (M10).
 *
 * Kept out of the component because it is all pure: the server sends derived
 * `(date, supplier)` rows and classifiable contents, and the board has to turn
 * them into the two-level layout of the wireframe — 日期 header, then the
 * supplier groups under it — without inventing a group that nobody decided.
 *
 * ⚠️ Two rules this file exists to hold on to:
 *
 * 1. **A suggestion is never a decision.** `decidedOf()` reads only
 *    `decided_*`, `suggestionsOf()` reads only `candidates`. They are kept
 *    apart here so the template cannot accidentally show a candidate as a
 *    settled value (`docs/multifiles.md` section 4).
 * 2. **Empty is not "failed".** A content with no supplier may never have been
 *    read, may have been read with nothing on the paper, or may have failed.
 *    `recognitionNote()` says which one, instead of one flat 「未知」.
 */

import type { IntakeCandidate, IntakeGroup, IntakeItem } from '../../shared/api'

/** Where a move is headed. `date: null, source: null` is 待归类. */
export interface BoardTarget {
  purchaseDate: string | null
  sourceName: string | null
}

export interface SupplierGroup extends BoardTarget {
  itemCount: number
}

export interface DateSection {
  /** Never null: a section exists because a date was decided. */
  purchaseDate: string
  itemCount: number
  /** Supplier groups plus, when it exists, the 商家待定 group of that date. */
  groups: SupplierGroup[]
}

export interface Board {
  /** The (null, null) bucket: nothing decided yet. */
  pendingCount: number
  /** One section per decided date, in the server's order. */
  dated: DateSection[]
  /** Supplier decided, date still missing — reachable only by fixing the date. */
  undated: SupplierGroup[]
}

export function buildBoard(groups: IntakeGroup[]): Board {
  let pendingCount = 0
  const dated: DateSection[] = []
  const undated: SupplierGroup[] = []
  const sections = new Map<string, DateSection>()
  for (const group of groups) {
    const entry: SupplierGroup = {
      purchaseDate: group.purchase_date, sourceName: group.source_name, itemCount: group.item_count
    }
    if (group.purchase_date === null) {
      if (group.source_name === null) pendingCount = group.item_count
      else undated.push(entry)
      continue
    }
    let section = sections.get(group.purchase_date)
    if (!section) {
      section = { purchaseDate: group.purchase_date, itemCount: 0, groups: [] }
      sections.set(group.purchase_date, section)
      dated.push(section)
    }
    section.itemCount += group.item_count
    section.groups.push(entry)
  }
  return { pendingCount, dated, undated }
}

/** `2026-11-08` → `11 月 8 日`. Anything unexpected is shown as it arrived. */
export function monthDayLabel(iso: string | null): string {
  const match = iso ? /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso) : null
  if (!match) return iso ?? '日期待定'
  return `${Number(match[2])} 月 ${Number(match[3])} 日`
}

/** The year, shown next to the label so a batch spanning years is not ambiguous. */
export function yearLabel(iso: string | null): string {
  const match = iso ? /^(\d{4})-\d{2}-\d{2}$/.exec(iso) : null
  return match ? match[1] : ''
}

/**
 * True for a date this app will send as `purchase_date`.
 *
 * The calendar check matters: `2026-02-31` parses in some engines and the
 * server would reject it after the user already thought they were done.
 */
export function isIsoDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const [year, month, day] = value.split('-').map(Number)
  if (month < 1 || month > 12 || day < 1) return false
  return day <= new Date(Date.UTC(year, month, 0)).getUTCDate()
}

/** `第 3 页` / `第 12 行 · Sheet1` — the coordinate for looking at the original. */
export function cardLocation(item: IntakeItem): string {
  if (item.kind === 'page') return `第 ${item.page_no ?? '?'} 页`
  const sheet = item.sheet_name ? ` · ${item.sheet_name}` : ''
  return `第 ${item.row_no ?? '?'} 行${sheet}`
}

/** What a person has actually decided about this content. */
export function decidedOf(item: IntakeItem): { date: string | null; source: string | null } {
  return { date: item.decided_purchase_date, source: item.decided_source_name }
}

/**
 * True once M08 handed this content to review.
 *
 * ⚠️ The server is still the gate — a move of a handed-off content is a 409
 * either way. This flag exists so the page can **say so first**: the date and
 * supplier of something already in review are changed through the review's
 * classification revision, which also updates any purchase that came from it.
 * Without the flag the card looks movable and the user only finds out by
 * being refused (`docs/multifiles.md` M10 验证记录 ④ → M12).
 */
export function isHandedOff(item: IntakeItem): boolean {
  return item.handed_off_at !== null
}

/**
 * Why this selection cannot be handed to review yet, in the user's words, or
 * `''` when it can.
 *
 * These are the server's own rules (`services/intake_reviews.py` handoff), said
 * before the request rather than after: one draft is one explicitly chosen set
 * of 1—20 contents from a single confirmed (date, supplier) group, pages and
 * spreadsheet rows kept apart. A 日期/商家 group is **not** an invoice, so this
 * never sends "the whole group" by itself.
 */
export function handoffProblem(items: IntakeItem[]): string {
  if (!items.length) return '先选中要送审的内容。'
  if (items.length > 20) return '一次最多送审 20 项，请分几次送。'
  if (items.some(isHandedOff)) return '选中的内容里有已经送审的，请取消选中它们。'
  if (items.some((item) => !item.decided_purchase_date || !item.decided_source_name)) {
    return '先确认采购日期和商家，再送入审核。'
  }
  const groups = new Set(items.map((item) => `${item.decided_purchase_date}|${item.decided_source_name}`))
  if (groups.size !== 1) return '一次送审只能选同一天、同一商家的内容。'
  if (new Set(items.map((item) => item.kind)).size !== 1) {
    return '表格行和图片/PDF 页要分别送审。'
  }
  return ''
}

/** `缺日期和商家` / `缺商家` / `缺日期` / `''` — what is still missing. */
export function missingLabel(item: IntakeItem): string {
  const noDate = item.decided_purchase_date === null
  const noSource = item.decided_source_name === null
  if (noDate && noSource) return '缺日期和商家'
  if (noSource) return '缺商家'
  if (noDate) return '缺日期'
  return ''
}

const BASIS_TEXT: Record<IntakeCandidate['basis'], string> = {
  paper: '纸面读出',
  same_file: '参考同文件',
  history: '与历史写法相近',
  similar_layout: '与历史单据外观相似'
}

export interface SuggestionChip {
  id: number
  field: IntakeCandidate['field']
  /** What the suggestion says, already in display form. */
  text: string
  /** Why it was suggested — the user decides whether that reason is good enough. */
  basis: string
  /** The raw value a move would write. */
  value: string
}

/**
 * The suggestions for one content, best first.
 *
 * Only suggestions for a field **nobody has decided yet** are offered: once a
 * person has written the supplier, a candidate for it is history, not a choice.
 */
export function suggestionsOf(item: IntakeItem): SuggestionChip[] {
  const chips: SuggestionChip[] = []
  for (const candidate of item.candidates) {
    if (candidate.value === null) continue
    if (candidate.field === 'purchase_date' && item.decided_purchase_date !== null) continue
    if (candidate.field === 'source_name' && item.decided_source_name !== null) continue
    chips.push({
      id: candidate.id,
      field: candidate.field,
      text: candidate.field === 'purchase_date'
        ? `${monthDayLabel(candidate.value)}${yearLabel(candidate.value) ? `（${yearLabel(candidate.value)}）` : ''}`
        : candidate.value,
      basis: BASIS_TEXT[candidate.basis] ?? candidate.basis,
      value: candidate.value
    })
  }
  return chips
}

/**
 * One honest sentence about the reading of this content.
 *
 * ⚠️ `status === null` is 「还没读这张」, not 「读不出来」: the recognition queue
 * is driven separately from page splitting, and saying the wrong one would send
 * the user looking for a problem that is not there.
 */
export function recognitionNote(item: IntakeItem): string {
  const reading = item.recognition
  if (reading.error) return `读取失败：${reading.error.message}`
  if (reading.status === null) return '还没读这张纸'
  if (reading.status === 'pending') return '正在读这张纸'
  if (reading.status === 'failed') return '这张纸读取失败'
  const notes: string[] = []
  if (!item.candidates.some((candidate) => candidate.field === 'source_name')) {
    notes.push('读过了，纸面上没找到商家')
  }
  if (reading.date_ambiguous) {
    notes.push(`纸面日期「${reading.date_raw_text ?? ''}」有两种读法，要人工确认`)
  } else if (reading.date_problem) {
    notes.push(`纸面日期读不出来：${reading.date_problem}`)
  } else if (!item.candidates.some((candidate) => candidate.field === 'purchase_date')) {
    notes.push('读过了，纸面上没找到日期')
  }
  return notes.join('；')
}

/** 「演示识别结果」 belongs on anything a fake OCR produced. */
export function isDemoReading(item: IntakeItem): boolean {
  return item.recognition.is_fake === true
}

/**
 * A place a card can be dropped or sent.
 *
 * ⚠️ `date` (the 日期 header) and `group` **with `sourceName: null`** (the
 * 商家待定 row under that date) write exactly the same thing — only the date.
 * They stay two zones so that hovering one does not highlight the other, and
 * so the wireframe's two drop levels keep their own wording.
 */
export type DropZone =
  | { kind: 'pending' }
  | { kind: 'date'; purchaseDate: string }
  | { kind: 'group'; purchaseDate: string; sourceName: string | null }

/** Identity of a bucket of contents. JSON keeps supplier names unescaped-safe. */
export function bucketKey(target: BoardTarget): string {
  return JSON.stringify([target.purchaseDate, target.sourceName])
}

export function dropKey(zone: DropZone): string {
  if (zone.kind === 'pending') return JSON.stringify(['pending'])
  if (zone.kind === 'date') return JSON.stringify(['date', zone.purchaseDate])
  return JSON.stringify(['group', zone.purchaseDate, zone.sourceName])
}

export function parseDropKey(key: string | null): DropZone | null {
  if (!key) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(key)
  } catch {
    return null
  }
  if (!Array.isArray(parsed)) return null
  const [kind, date, source] = parsed as [unknown, unknown, unknown]
  if (kind === 'pending') return { kind: 'pending' }
  if (kind === 'date' && typeof date === 'string') return { kind: 'date', purchaseDate: date }
  if (kind === 'group' && typeof date === 'string' && (typeof source === 'string' || source === null)) {
    return { kind: 'group', purchaseDate: date, sourceName: source }
  }
  return null
}

/** The move this zone performs. `clear` sends the contents back to 待归类. */
export function zoneAction(zone: DropZone): 'clear' | 'date' | 'group' {
  if (zone.kind === 'pending') return 'clear'
  if (zone.kind === 'date' || zone.sourceName === null) return 'date'
  return 'group'
}

export function zoneTarget(zone: DropZone): BoardTarget {
  if (zone.kind === 'pending') return { purchaseDate: null, sourceName: null }
  if (zone.kind === 'date') return { purchaseDate: zone.purchaseDate, sourceName: null }
  return { purchaseDate: zone.purchaseDate, sourceName: zone.sourceName }
}

/**
 * True when this content already holds what the move would write — dropping it
 * where it already is must not spend a move the user would then try to undo.
 */
export function alreadyThere(item: IntakeItem, zone: DropZone): boolean {
  const action = zoneAction(zone)
  const target = zoneTarget(zone)
  if (action === 'clear') {
    return item.decided_purchase_date === null && item.decided_source_name === null
  }
  if (action === 'date') return item.decided_purchase_date === target.purchaseDate
  return item.decided_purchase_date === target.purchaseDate
    && item.decided_source_name === target.sourceName
}

/** 「11 月 8 日 / Costco」 — used in the undo banner, so it names the real target. */
export function zoneLabel(zone: DropZone): string {
  if (zone.kind === 'pending') return '待归类'
  const date = monthDayLabel(zone.purchaseDate)
  const year = yearLabel(zone.purchaseDate)
  const dated = `${date}${year ? `（${year}）` : ''}`
  if (zone.kind === 'date' || zone.sourceName === null) return `${dated} / 商家待定`
  return `${dated} / ${zone.sourceName}`
}
