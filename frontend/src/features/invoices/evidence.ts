/**
 * Reading a draft line's `raw_fields`.
 *
 * `raw_fields` is handed over by the backend exactly as the extraction wrote it
 * (`backend/src/api/schemas.py`: "原样给前端"), so it is free-form JSON here and
 * every read has to be defensive. Three shapes reach this file:
 *
 * | Where the line came from | What `raw_fields` holds |
 * | --- | --- |
 * | OCR / text layer | `fields.<ocr name>` with `status` / `raw_text` / `box` / `box_source` / `model_values` / `agreement`, plus `grid`, `printed_row`, `file_id` |
 * | spreadsheet import | `source: "spreadsheet"` with `sheet` / `row` / `cells` and `fields.<business name>` — **no coordinates at all** |
 * | manually added line | `{}` |
 *
 * The one rule that matters: a field without a `box` has **no coordinates**, and
 * this module never invents one. Guessing a row would make the backend crop the
 * neighbouring line's handwriting and return it with a 200 — the error nobody
 * would catch.
 */
import type { CropField, InvoiceLine } from '../../shared/api'

/** The five cells the crop endpoint knows, in the order the form shows them. */
export const EVIDENCE_FIELDS = [
  { crop: 'item_description', label: '商品名称', sheet: 'description' },
  { crop: 'quantity', label: '数量', sheet: 'quantity' },
  { crop: 'unit', label: '采购单位', sheet: 'purchase_unit' },
  { crop: 'unit_price', label: '单价', sheet: 'unit_price' },
  { crop: 'line_amount', label: '行金额', sheet: 'line_amount' }
] as const satisfies readonly { crop: CropField; label: string; sheet: string }[]

export type BoxSource = 'grid' | 'grid_row' | 'vendor'

export interface FieldEvidence {
  field: CropField
  label: string
  /** `recognized` / `unreadable` / `missing`, or null when nothing was recorded. */
  status: string | null
  rawText: string | null
  confidence: number | null
  page: number | null
  /** True only when the backend recorded real coordinates for this cell. */
  hasBox: boolean
  boxSource: BoxSource | null
  /** Each model's reading, in model order. Empty for single-model paths. */
  modelValues: string[]
  /** The cross-model verdict, in the backend's own words. */
  agreement: string | null
  /** Spreadsheet imports locate a value by cell, not by pixels. */
  cell: string | null
}

export interface GridBand {
  index: number
  /** Ratio of page height / width, as the backend recorded it. */
  start: number
  end: number
}

export interface GridInfo {
  rowAnchor: string | null
  rowAnchorNote: string | null
  rowCapacity: number | null
  columnCount: number | null
  rows: GridBand[]
  columns: GridBand[]
}

export interface LineEvidenceView {
  /** 'ocr' | 'spreadsheet' | 'manual' — which of the three shapes this is. */
  origin: 'ocr' | 'spreadsheet' | 'manual'
  fileId: number | null
  intakeItemId: number | null
  /** Page the line sits on, from the evidence or the line's own `page`. */
  page: number | null
  printedRow: number | null
  rowBand: number | null
  provider: string | null
  models: string[]
  promptSha12: string | null
  lineText: string | null
  fields: FieldEvidence[]
  grid: GridInfo | null
  /** Sheet / row for an imported line. */
  sheet: string | null
  sheetRow: number | null
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

function asText(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function asInteger(value: unknown): number | null {
  const number = asNumber(value)
  return number !== null && Number.isSafeInteger(number) ? number : null
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function boxSourceOf(value: unknown): BoxSource | null {
  return value === 'grid' || value === 'grid_row' || value === 'vendor' ? value : null
}

function bands(value: unknown, startKey: string, endKey: string): GridBand[] {
  if (!Array.isArray(value)) return []
  const list: GridBand[] = []
  for (const entry of value) {
    const band = asRecord(entry)
    const index = asInteger(band.index)
    const start = asNumber(band[startKey])
    const end = asNumber(band[endKey])
    if (index !== null && start !== null && end !== null) list.push({ index, start, end })
  }
  return list
}

function parseGrid(value: unknown): GridInfo | null {
  const grid = asRecord(value)
  if (Object.keys(grid).length === 0) return null
  return {
    rowAnchor: asText(grid.row_anchor),
    rowAnchorNote: asText(grid.row_anchor_note),
    rowCapacity: asInteger(grid.row_capacity),
    columnCount: asInteger(grid.column_count),
    rows: bands(grid.rows, 'top', 'bottom'),
    columns: bands(grid.columns, 'left', 'right')
  }
}

/** Reads one line's evidence. Never throws: a shape it does not know becomes "manual". */
export function readLineEvidence(line: InvoiceLine): LineEvidenceView {
  const raw = asRecord(line.raw_fields)
  const fields = asRecord(raw.fields)
  const spreadsheet = raw.source === 'spreadsheet'
  const origin: LineEvidenceView['origin'] = spreadsheet
    ? 'spreadsheet'
    : Object.keys(fields).length > 0 ? 'ocr' : 'manual'

  const parsed: FieldEvidence[] = EVIDENCE_FIELDS.map((entry) => {
    const payload = asRecord(fields[spreadsheet ? entry.sheet : entry.crop])
    const column = asText(payload.column)
    const sheetRow = asInteger(raw.row)
    return {
      field: entry.crop,
      label: entry.label,
      status: asText(payload.status),
      rawText: typeof payload.raw_text === 'string' ? payload.raw_text : null,
      confidence: asNumber(payload.confidence),
      page: asInteger(payload.page),
      // ⚠️ Spreadsheet evidence has no pixels, so `hasBox` must stay false even
      // though `fields.<name>` exists: there is nothing to crop out of a .csv.
      hasBox: !spreadsheet && Object.keys(asRecord(payload.box)).length > 0,
      boxSource: spreadsheet ? null : boxSourceOf(payload.box_source),
      modelValues: asStringList(payload.model_values),
      agreement: asText(payload.agreement),
      cell: spreadsheet && column ? `${column}${sheetRow ?? ''}` : null
    }
  })

  return {
    origin,
    fileId: asInteger(raw.file_id),
    intakeItemId: asInteger(raw.intake_item_id),
    page: asInteger(raw.page) ?? parsed.find((item) => item.page !== null)?.page ?? line.page,
    printedRow: asInteger(raw.printed_row),
    rowBand: asInteger(raw.row_band),
    provider: asText(raw.provider),
    models: asStringList(raw.models),
    promptSha12: asText(raw.prompt_sha12),
    lineText: asText(raw.line_text),
    fields: parsed,
    grid: spreadsheet ? null : parseGrid(raw.grid),
    sheet: spreadsheet ? asText(raw.sheet) : null,
    sheetRow: spreadsheet ? asInteger(raw.row) : null
  }
}

/** A suggestion is display-only until the reviewer accepts it. */
export function suggestedValue(line: InvoiceLine, field: CropField): string | null {
  const evidence = readLineEvidence(line).fields.find((item) => item.field === field)
  if (!evidence || evidence.status === 'missing' || evidence.status === 'invalid') return null
  const raw = evidence.rawText?.trim()
  if (evidence.status === 'recognized' && raw) {
    if (field === 'item_description') return cleanProductName(raw)
    if ((field === 'quantity' || field === 'unit_price' || field === 'line_amount')
        && !/^[0-9]+(?:\.[0-9]+)?$/.test(raw)) return null
    return raw
  }

  const values = [...new Set(evidence.modelValues.map((value) => value.trim()).filter(Boolean))]
  if (values.length < 2) return null // One model alone did not settle an unreadable cell.
  if (field === 'quantity' || field === 'unit_price' || field === 'line_amount') return null
  const rank = (value: string) => /^[A-Za-z\s]+$/.test(value) ? 0
    : /^[\p{Script=Han}\s]+$/u.test(value) ? 1 : 2
  const letters = (value: string) => (value.match(/[A-Za-z\p{Script=Han}]/gu) ?? []).length
  const selected = values.sort((a, b) => rank(a) - rank(b) || letters(b) - letters(a) || b.length - a.length)[0]
  return field === 'item_description' ? cleanProductName(selected ?? '') : selected ?? null
}

function cleanProductName(value: string): string | null {
  // Numbers on the paper often belong to a row number or a pack size (17X30).
  // Drop those whole tokens before removing the remaining punctuation.
  const clean = value.normalize('NFKC')
    .replace(/[A-Za-z]*\d+[A-Za-z\d]*/g, ' ')
    .replace(/[^A-Za-z\p{Script=Han}\s]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  return clean || null
}

export const FIELD_STATUS_LABEL: Record<string, string> = {
  recognized: '识别出内容',
  unreadable: '这一格认不出',
  missing: '单据上没有这一格',
  invalid: '原文解析不了'
}

/** The five invoice-header cells (backend `src/contracts.py` DOCUMENT_FIELDS). */
const DOCUMENT_FIELDS = [
  { key: 'supplier', label: '来源' },
  { key: 'supplier_english', label: '纸面英文商家名' },
  { key: 'supplier_chinese', label: '纸面中文商家名' },
  { key: 'supplier_evidence', label: '模型摘录的商家原文线索' },
  { key: 'invoice_date', label: '日期' },
  { key: 'invoice_number', label: '单号' },
  { key: 'invoice_total', label: '总额' },
  { key: 'currency', label: '币种' }
] as const

export interface DocumentFieldEvidence {
  key: string
  label: string
  status: string | null
  rawText: string | null
  page: number | null
  modelValues: string[]
  agreement: string | null
}

export interface DocumentEvidenceView {
  docClass: string | null
  model: string | null
  warnings: string[]
  fields: DocumentFieldEvidence[]
}

/**
 * Reads the invoice header's recognition evidence.
 *
 * Why it is worth showing: an `invoice_date` of null next to a raw text here
 * means "the paper says something, it just is not a calendar date" — which is a
 * different thing from "nothing was recognised", and only the evidence tells
 * them apart.
 */
export function readDocumentEvidence(raw: unknown): DocumentEvidenceView | null {
  const payload = asRecord(raw)
  const fields = asRecord(payload.fields)
  if (Object.keys(payload).length === 0) return null
  return {
    docClass: asText(payload.doc_class),
    model: asText(payload.model),
    warnings: asStringList(payload.warnings),
    fields: DOCUMENT_FIELDS.map((entry) => {
      const found = asRecord(fields[entry.key])
      return {
        key: entry.key,
        label: entry.label,
        status: asText(found.status),
        rawText: typeof found.raw_text === 'string' ? found.raw_text : null,
        page: asInteger(found.page),
        modelValues: asStringList(found.model_values),
        agreement: asText(found.agreement)
      }
    }).filter((item) => item.status !== null || item.rawText !== null)
  }
}
