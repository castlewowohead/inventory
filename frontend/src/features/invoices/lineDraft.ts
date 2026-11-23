/**
 * The draft form behind one invoice line (F07).
 *
 * Why this is a module and not inline in the component: three rules here are
 * contract, not presentation, and all three are the kind that fail silently if
 * they drift.
 *
 * 1. **Numbers never become JS numbers.** They are entered, held, compared and
 *    sent as strings. `backend/src/services/purchases.py:parse_decimal` rejects
 *    a JSON decimal outright, and the reason is not pedantry: `JSON.parse` has
 *    already pushed `1234567.89` through a double before any of our code could
 *    look at it. So this file never calls `Number()` on a value it will store,
 *    and it never does arithmetic on one.
 * 2. **An empty box is `null`, not `0`.** The backend deliberately leaves a cell
 *    it could not read empty, and 0 in this product means a real thing (a free
 *    line, a zero-priced giveaway). Turning one into the other would invent a
 *    purchase price.
 * 3. **A partial save sends only the keys the user edited.** The backend's
 *    `field()` helper reads an absent key as "keep" and an explicit `null` as
 *    "set to unknown"; spelling out untouched fields would let a stale form
 *    overwrite a value someone else just changed, which is exactly what the
 *    optimistic lock exists to prevent.
 */
import type { InvoiceLine, InvoiceLinePatch, ReviewIssue } from '../../shared/api'

/** The editable fields, as strings. `''` means "unknown" (sent as `null`). */
export interface LineDraft {
  description: string
  quantity: string
  purchase_unit: string
  unit_price: string
  line_amount: string
  purchase_date: string
  source_name: string
  currency: string
  line_kind: 'purchase' | 'adjustment'
}

export type DraftField = keyof Omit<LineDraft, 'line_kind'>

/** Decimal scales, straight from `backend/src/models.py`. */
const SCALE: Record<string, number> = { quantity: 4, unit_price: 4, line_amount: 2 }
const MAX_INTEGER_DIGITS = 12
/** `DECIMAL_PATTERN` in purchases.py, ASCII-only for the same reason: a full-width
 *  `６.００` is a different spelling of the same number, and `Decimal` accepts it. */
const DECIMAL = /^[+-]?[0-9]+(\.[0-9]+)?$/

export const FIELD_LABEL: Record<DraftField | 'line_kind' | 'food_item_id', string> = {
  description: '商品名称',
  quantity: '数量',
  purchase_unit: '采购单位',
  unit_price: '单价',
  line_amount: '行金额',
  purchase_date: '日期',
  source_name: '来源',
  currency: '币种',
  line_kind: '行类型',
  food_item_id: '食材'
}

/** Max lengths, from `backend/src/models.py`. */
const TEXT_MAX: Record<string, number> = {
  description: 300, purchase_unit: 32, source_name: 120
}

export function draftOf(line: InvoiceLine): LineDraft {
  return {
    description: line.description ?? '',
    quantity: line.quantity ?? '',
    purchase_unit: line.purchase_unit ?? '',
    unit_price: line.unit_price ?? '',
    line_amount: line.line_amount ?? '',
    purchase_date: line.purchase_date ?? '',
    source_name: line.source_name ?? '',
    currency: line.currency ?? '',
    line_kind: line.line_kind
  }
}

/** The fields whose form value differs from what the server last returned. */
export function changedFields(draft: LineDraft, line: InvoiceLine): DraftField[] {
  const stored = draftOf(line)
  const keys = Object.keys(stored).filter((key) => key !== 'line_kind') as DraftField[]
  return keys.filter((key) => draft[key] !== stored[key])
}

/**
 * Local checks before a save, mirroring the backend's own rules so the user is
 * told at the field instead of by a round trip. The backend still decides: this
 * never widens what it accepts, and anything it rejects is shown verbatim.
 */
export function validateDraft(draft: LineDraft): Partial<Record<DraftField, string>> {
  const errors: Partial<Record<DraftField, string>> = {}
  const adjustment = draft.line_kind === 'adjustment'

  for (const name of ['quantity', 'unit_price', 'line_amount'] as const) {
    const text = draft[name].trim()
    if (!text) continue
    if (!DECIMAL.test(text)) {
      errors[name] = `请写成 "12.34" 这样的半角数字，不要带货币符号、千分位逗号或科学计数法。留空表示未知。`
      continue
    }
    const [whole, fraction = ''] = text.replace(/^[+-]/, '').split('.')
    if (fraction.length > SCALE[name]) {
      errors[name] = `最多 ${SCALE[name]} 位小数。超出的位数不会被四舍五入掉，请自己决定存哪个值。`
      continue
    }
    if (whole.replace(/^0+(?=\d)/, '').length > MAX_INTEGER_DIGITS) {
      errors[name] = `整数部分最多 ${MAX_INTEGER_DIGITS} 位。`
      continue
    }
    const negative = text.startsWith('-') && /[1-9]/.test(text)
    const zero = !/[1-9]/.test(text)
    if (negative && !adjustment) {
      errors[name] = '采购行不能填写负数；请核对原件。'
    } else if (zero && name === 'quantity' && !adjustment) {
      errors[name] = '采购行的数量必须大于 0。真的没有数量就留空（未知），不要写 0。'
    }
  }

  for (const name of ['description', 'purchase_unit', 'source_name'] as const) {
    const text = draft[name].trim()
    if (text.length > TEXT_MAX[name]) errors[name] = `最多 ${TEXT_MAX[name]} 个字符。`
  }

  const date = draft.purchase_date.trim()
  if (date && !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    errors.purchase_date = '请写成 YYYY-MM-DD；留空表示跟随单据头。'
  }
  return errors
}

function text(value: string): string | null {
  return value.trim() || null
}

/**
 * The PATCH body for exactly the fields that changed.
 *
 * `version` is always included: a save without it would be accepted blindly
 * (`_check_version` skips the check when it is absent), which is the overwrite
 * the plan forbids.
 */
export function patchBody(draft: LineDraft, line: InvoiceLine): InvoiceLinePatch | null {
  const changed = changedFields(draft, line)
  if (!changed.length) return null
  const body: InvoiceLinePatch = { version: line.version }
  for (const key of changed) {
    if (key === 'currency') body.currency = text(draft.currency)
    else body[key] = text(draft[key])
  }
  return body
}

/** A new hand-added line starts as a purchase line with every cell unknown. */
export const BLANK_LINE_INPUT = { line_kind: 'purchase' } as const

/**
 * 把识别出来的纸面金额写成这些输入框收得下的样子（多文件计划 M12）。
 *
 * 纸上印的是「$1,040.00」，而金额框只收 `1040.00`——这是上面第 1 条规则的延伸，
 * 也是校验里写明的「不要带货币符号、千分位逗号」。问题出在「此数据正确」上：
 * 它接受的是**模型读到的纸面原文**，照单收下就会存不进去，而失败只会在别处
 * 显示成「请先检查并保存单据信息」，人看不出到底哪里不对。真实 OCR 跑这条链路
 * 时就是这样卡住的。
 *
 * 只去掉货币符号、空格和**千分位**逗号。去不干净的（例如欧式写法「1.040,00」）
 * 原样返回，让字段自己的报错照常出现——这里宁可让人自己决定，也不替人猜一个
 * 金额。
 */
export function plainAmount(value: string): string {
  const stripped = value
    .normalize('NFKC')
    .replace(/[\s$¥￥€£]/g, '')
    .replace(/,(?=\d{3}(?:\D|$))/g, '')
  return DECIMAL.test(stripped) ? stripped : value
}

/**
 * How one value should read in the form.
 *
 * `unknown` and `zero` are different states on purpose: the first means nobody
 * knows yet, the second is a number someone decided on. Showing them the same
 * way is how a blank cell turns into a $0 purchase price.
 */
export function valueState(raw: string | null): 'unknown' | 'zero' | 'filled' {
  if (raw === null || raw.trim() === '') return 'unknown'
  return /^[+-]?0+(?:\.0+)?$/.test(raw.trim()) ? 'zero' : 'filled'
}

/** The issue list, grouped by the field that clears it when PATCHed. */
export function issuesByField(issues: ReviewIssue[]): Map<string, ReviewIssue[]> {
  const grouped = new Map<string, ReviewIssue[]>()
  for (const issue of issues) {
    const key = typeof issue.field === 'string' ? issue.field : ''
    grouped.set(key, [...(grouped.get(key) ?? []), issue])
  }
  return grouped
}

/** The fields whose issues the line form renders next to their own input. */
export const ISSUE_FIELDS = new Set([
  "description", "quantity", "purchase_unit", "unit_price", "line_amount"
])

export function missingLabel(name: string): string {
  return FIELD_LABEL[name as DraftField] ?? name
}
