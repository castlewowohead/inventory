import { apiRequest, apiRequestBinary, type BinaryResponse } from './request'
import type {
  Category, ExtractionJob, Health, ItemList, Page, FoodItem, FoodItemSuggestions, MergePlan, Invoice, InvoiceLine, MatchCandidate, Purchase, PriceUnits, PriceHistory, MonthlyPrices, WeeklyPrices, SourcePrices, PriceAlerts, AlertSettings, IntakeBatch, IntakeBatchFile, IntakeGroupPage, IntakeItem,
  IntakeMoveResult
} from './types'

export { ApiError, errorMessage, userMessage } from './errors'
export { apiRequestBinary } from './request'
export type { BinaryResponse } from './request'
export { apiRequest } from './request'
export type * from './types'

export const getHealth = () => apiRequest<Health>('/health')
export const getCategories = () => apiRequest<{ items: Category[] }>('/api/categories')
export const createCategory = (data: { name: string; name_zh: string | null }) =>
  apiRequest<Category>('/api/categories', { method: 'POST', data })
export const updateCategory = (id: number, data: { name: string; name_zh: string | null }) =>
  apiRequest<Category>(`/api/categories/${id}`, { method: 'PATCH', data })
export const deleteCategory = (id: number) =>
  apiRequest<void>(`/api/categories/${id}`, { method: 'DELETE' })
export const getFoodItems = (query = '') =>
  apiRequest<Page<FoodItem>>(`/api/food-items${query}`)
export const getFoodItemSuggestions = (query = '') => {
  const params = new URLSearchParams()
  if (query.trim()) params.set('q', query.trim())
  return apiRequest<FoodItemSuggestions>(`/api/food-items/suggest${params.size ? `?${params}` : ''}`)
}
export const getFoodItem = (id: number) =>
  apiRequest<FoodItem>(`/api/food-items/${id}`)
export interface FoodItemInput {
  name: string
  category_id: number
  base_unit: string
  name_zh: string | null
  specification: string | null
}
export const createFoodItem = (data: FoodItemInput) =>
  apiRequest<FoodItem>('/api/food-items', { method: 'POST', data: { ...data } })
export const updateFoodItem = (id: number, data: FoodItemInput) =>
  apiRequest<FoodItem>(`/api/food-items/${id}`, { method: 'PATCH', data: { ...data } })
export const deleteFoodItem = (id: number) =>
  apiRequest<void>(`/api/food-items/${id}`, { method: 'DELETE' })
export const previewFoodItemMerge = (targetId: number, sourceIds: number[]) =>
  apiRequest<MergePlan>(`/api/food-items/${targetId}/merge-preview`, { method: 'POST', data: { source_ids: sourceIds } })
export const mergeFoodItems = (targetId: number, sourceIds: number[]) =>
  apiRequest<MergePlan>(`/api/food-items/${targetId}/merge`, { method: 'POST', data: { source_ids: sourceIds } })
export const getInvoices = (query = '') =>
  apiRequest<Page<Invoice>>(`/api/invoices${query}`)
export const getInvoice = (id: number) =>
  apiRequest<Invoice>(`/api/invoices/${id}`)
export const getPurchase = (id: number) =>
  apiRequest<Purchase>(`/api/purchases/${id}`)
export interface InvoiceHeaderInput {
  source_name: string | null
  invoice_number: string | null
  invoice_date: string | null
  currency: string | null
  invoice_total: string | null
}
export const updateInvoice = (id: number, data: Partial<InvoiceHeaderInput>) =>
  apiRequest<Invoice>(`/api/invoices/${id}`, { method: 'PATCH', data: { ...data } })
export const voidInvoice = (id: number, confirmedLineIds?: number[]) =>
  apiRequest<{ status: 'voided'; message: string; invoice: Invoice }>(
    `/api/invoices/${id}`, { method: 'DELETE', ...(confirmedLineIds ? { data: { confirmed_line_ids: confirmedLineIds } } : {}) })
export const deleteInvoiceFile = (invoiceId: number, fileId: number) =>
  apiRequest<Invoice>(`/api/invoices/${invoiceId}/files/${fileId}`, { method: 'DELETE' })

/** Extraction jobs, newest first. Querying never starts a job. */
export const getExtractionJobs = (invoiceId: number) =>
  apiRequest<ItemList<ExtractionJob>>(`/api/invoices/${invoiceId}/extraction`)
/** Starts extraction for every recognizable file on the invoice (202). */
export const startExtraction = (invoiceId: number) =>
  apiRequest<ItemList<ExtractionJob>>(`/api/invoices/${invoiceId}/extraction`, { method: 'POST' })

/** 一行里的五个字段（后端 `src/contracts.py` LINE_FIELDS）。 */
export type CropField = 'item_description' | 'quantity' | 'unit' | 'unit_price' | 'line_amount'

/** The original bytes of one uploaded file, exactly as they were stored. */
export const getInvoiceFileContent = (invoiceId: number, fileId: number) =>
  apiRequestBinary(`/api/invoices/${invoiceId}/files/${fileId}/content`)

export const getIntakeItemOriginal = (batchId: number, itemId: number) =>
  apiRequestBinary(`/api/intake-batches/${batchId}/items/${itemId}/original`)
export const deleteIntakeItem = (batchId: number, itemId: number, version: number) =>
  apiRequest<{ deleted: true; item_id: number }>(
    `/api/intake-batches/${batchId}/items/${itemId}`, { method: 'DELETE', data: { version } })

export const createIntakeBatch = () =>
  apiRequest<IntakeBatch>('/api/intake-batches', { method: 'POST' })
export const getCurrentIntakeBatch = () =>
  apiRequest<IntakeBatch>('/api/intake-batches/current')
export const getIntakeBatch = (id: number) =>
  apiRequest<IntakeBatch>(`/api/intake-batches/${id}`)
/**
 * Ends this round of multi-file filling (plan section 22): the batch is closed,
 * so `/current` stops returning it and the home screen’s 「接着整理」 goes away.
 *
 * ⚠️ The server does **not** require everything to be filled in — section 5 lets
 * leftovers wait. 「全部填完才按得动」 is a gate in the board, not a new rule.
 * 409 when the batch was already closed.
 */
export const closeIntakeBatch = (id: number) =>
  apiRequest<IntakeBatch>(`/api/intake-batches/${id}/close`, { method: 'POST' })
export const getIntakeBatchFileOriginal = (batchId: number, fileId: number) =>
  apiRequestBinary(`/api/intake-batches/${batchId}/files/${fileId}/original`)
/**
 * Takes one wrongly uploaded file back out of the batch — a real delete (plan
 * section 16): its pages, their date/supplier decisions and the undo journal go
 * with it, and uploading the same file again starts from scratch. 409 when any of
 * its pages is already in a draft. `file` is what the reference looked like before
 * it went.
 */
export const discardIntakeBatchFile = (batchId: number, fileId: number) =>
  apiRequest<{
    file: IntakeBatchFile; removed_item_count: number; bytes_still_referenced: boolean
  }>(`/api/intake-batches/${batchId}/files/${fileId}`, { method: 'DELETE' })
export const processIntakeBatch = (id: number, retryFailed = false) =>
  apiRequest<{ batch: IntakeBatch }>(`/api/intake-batches/${id}/process`, {
    method: 'POST', data: { retry_failed: retryFailed }
  })
/** Reads date and supplier suggestions for queued pages; never confirms a group. */
export const recognizeIntakeBatch = (id: number, retryFailed = false, limit = 1) =>
  apiRequest<{ results: { outcome: string }[]; model_calls: number }>(
    `/api/intake-batches/${id}/recognize?limit=${limit}`,
    { method: 'POST', data: { retry_failed: retryFailed }, timeout: 600000 }
  )

/** Every classifiable content in the batch, newest decisions included. */
export interface IntakeItemFilters {
  purchaseDate?: string
  sourceName?: string
  missing?: 'any' | 'both' | 'date' | 'source' | 'date_any' | 'source_any' | 'recognition_failed'
}
export const getIntakeItems = (batchId: number, limit = 50, offset = 0, filters: IntakeItemFilters = {}) => {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (filters.purchaseDate) params.set('purchase_date', filters.purchaseDate)
  if (filters.sourceName) params.set('source_name', filters.sourceName)
  if (filters.missing) params.set('missing', filters.missing)
  return apiRequest<Page<IntakeItem>>(`/api/intake-batches/${batchId}/items?${params}`)
}

/** The derived (date, supplier) groups, unclassified bucket included. */
export const getIntakeGroups = (batchId: number, limit = 50, offset = 0) =>
  apiRequest<IntakeGroupPage>(`/api/intake-batches/${batchId}/groups?limit=${limit}&offset=${offset}&suggested=true`)

/**
 * The contents of one group. Leaving a parameter out addresses the null side:
 * no arguments at all is the unclassified bucket, a date alone is 商家待定.
 */
export const getIntakeGroupItems = (
  batchId: number,
  target: { purchaseDate?: string | null; sourceName?: string | null },
  limit = 50, offset = 0
) => {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset), suggested: 'true' })
  if (target.purchaseDate) params.set('purchase_date', target.purchaseDate)
  if (target.sourceName) params.set('source_name', target.sourceName)
  return apiRequest<Page<IntakeItem>>(`/api/intake-batches/${batchId}/groups/items?${params}`)
}

export const createIntakeDateGroup = (batchId: number, purchaseDate: string) =>
  apiRequest<{ purchase_date: string }>(`/api/intake-batches/${batchId}/date-groups`, {
    method: 'POST', data: { purchase_date: purchaseDate }
  })

export const deleteIntakeDateGroup = (batchId: number, purchaseDate: string) =>
  apiRequest<{ purchase_date: string; deleted: boolean }>(
    `/api/intake-batches/${batchId}/date-groups/${purchaseDate}`, { method: 'DELETE' })

/**
 * The one move command behind dragging, multi-select and tapping a target
 * (docs/multifiles.md section 4): `date` writes only the date, `group` writes
 * both, `clear` sends contents back to 待归类. `requestKey` makes a repeated
 * submission return the first result instead of moving twice.
 */
export const moveIntakeItems = (
  batchId: number,
  body: {
    request_key: string
    action: 'date' | 'group' | 'source' | 'clear'
    items: { id: number; version: number }[]
    purchase_date?: string
    source_name?: string
  }
) => apiRequest<IntakeMoveResult>(`/api/intake-batches/${batchId}/moves`, { method: 'POST', data: { ...body } })

/** Undoes the most recent move that has not been undone yet. */
export const undoIntakeMove = (batchId: number, moveId: number) =>
  apiRequest<IntakeMoveResult>(`/api/intake-batches/${batchId}/moves/${moveId}/undo`, { method: 'POST' })

/**
 * M08 的送审命令：把**明确选中的**内容做成一张待审核草稿（M12 把闭环接起来时
 * 卡片页与财务表格共用它）。
 *
 * 服务端的边界要照着说给用户听，而不是等 409：一次 1—20 条、同日期同商家、
 * 页与表格行不能混、纸面单号/总额不一致要分开送。重复送审返回原草稿
 * （`created: false`），所以重复点一下不会多出一张单据。
 */
export const handOffIntakeItems = (
  batchId: number,
  items: { id: number; version: number }[],
  targetInvoiceId?: number,
  suggestedGroup?: { purchase_date: string; source_name: string }
) => apiRequest<{ invoice_id: number; item_ids: number[]; created: boolean }>(
  `/api/intake-batches/${batchId}/reviews`,
  { method: 'POST', data: { items, ...(targetInvoiceId ? { target_invoice_id: targetInvoiceId } : {}),
    ...(suggestedGroup ? { suggested_group: suggestedGroup } : {}) } }
)

export const startIntakeForms = (
  batchId: number, items: { id: number; version: number }[],
  suggestedGroup: { purchase_date: string; source_name: string }
) => apiRequest<{ invoice_ids: number[]; item_ids: number[]; created: boolean }>(
  `/api/intake-batches/${batchId}/reviews/selection`,
  { method: 'POST', data: { items, suggested_group: suggestedGroup } }
)

/**
 * 撤回填表：丢掉草稿上填过的数据，内容回到「已分类、还没填表」。
 *
 * ⚠️ 服务端撤的是**整张草稿**（一张草稿就是一张发票），所以返回的 `item_ids`
 * 可能比传进去的多——同一张草稿里的兄弟页会一起回来。已经确认入账的行撤不了，
 * 那条路是单据里的撤销确认（第 11 节 ③）。
 */
export const withdrawIntakeForms = (
  batchId: number, items: { id: number; version: number }[]
) => apiRequest<{ item_ids: number[]; invoice_ids: number[]; cleared_lines: number }>(
  `/api/intake-batches/${batchId}/reviews/withdraw`, { method: 'POST', data: { items } }
)

/**
 * 重新填写：清掉这张单据填过的数据，为同一批页重新开一张空草稿（第 20 节）。
 *
 * ⚠️ 与 `withdrawIntakeForms` 的两点不同：①**已完成的单据可以走这条路**，它已经
 * 入账的采购记录在同一个事务里作废（不撤就会重复记价）；②清完立刻建一张新草稿，
 * 返回的 `invoice_id` 就是要落进去的那一张。日期和商家这些人工决定一个字不动。
 */
export const refillIntakeForm = (
  batchId: number, items: { id: number; version: number }[]
) => apiRequest<{
  invoice_id: number; previous_invoice_id: number; previous_invoice_kept: boolean
  item_ids: number[]; cleared_lines: number; voided_purchases: number
}>(`/api/intake-batches/${batchId}/reviews/refill`, { method: 'POST', data: { items } })

export const reviseIntakeReviewClassification = (
  batchId: number, invoiceId: number, items: { id: number; version: number }[],
  purchaseDate: string, sourceName: string
) => apiRequest(`/api/intake-batches/${batchId}/reviews/${invoiceId}/classification`, {
  method: 'PATCH', data: { scope: 'invoice', items, purchase_date: purchaseDate, source_name: sourceName }
})

/**
 * 填表期的**拆分 / 合并**：把选中的页挪到另一张草稿。
 *
 * 单据边界只认纸面单号，读不出单号就一页一张（docs/multifiles.md 第 12 节）。
 * 多拆出来的那一张就靠这条接口并回去；一张真的两页发票被拆开时也走这里。
 *
 * - 不给 `targetInvoiceId`：新建一张草稿，把选中的页搬过去（**拆分**）。
 * - 给了：搬进那张草稿（**合并**）。把这张草稿的页全搬走，它自己就作废。
 *
 * ⚠️ 搬完之后两边的**单据总额都会被清空**：拆开的两半各自该是多少，只有纸面
 * 知道；服务端绝不拿一个总额去除。已经确认入账的行搬不动，那条路是撤销确认。
 */
export const reassignIntakeReviewContents = (
  batchId: number, invoiceId: number, items: { id: number; version: number }[],
  targetInvoiceId?: number
) => apiRequest<{ source_invoice_id: number; target_invoice_id: number; item_ids: number[]; created: boolean }>(
  `/api/intake-batches/${batchId}/reviews/${invoiceId}/reassign`,
  { method: 'POST', data: targetInvoiceId === undefined ? { items } : { items, target_invoice_id: targetInvoiceId } }
)

function headerJson(value: string | undefined): unknown {
  if (!value) return null
  try {
    return JSON.parse(value)
  } catch {
    // A header we cannot parse is not a reason to lose the image.
    return null
  }
}

/** Both candidate endpoints answer with `{"candidates": [...]}`. */
export interface MatchCandidateList {
  candidates: MatchCandidate[]
}

/**
 * The business fields of one draft line.
 *
 * Every number is a **string or null**, never a JS number: the backend rejects
 * JSON decimals outright (`parse_decimal`), because `JSON.parse` has already
 * put `1234567.89` through a double by the time any code could read it. `null`
 * means "unknown" and is a value the draft is allowed to hold — it is not 0.
 */
export interface InvoiceLineInput {
  description?: string | null
  quantity?: string | null
  purchase_unit?: string | null
  unit_price?: string | null
  line_amount?: string | null
  food_item_id?: number | null
  line_kind?: 'purchase' | 'adjustment'
  purchase_date?: string | null
  source_name?: string | null
  currency?: string | null
}

/** PATCH also carries the optimistic-lock version and the skip fields. */
export interface InvoiceLinePatch extends InvoiceLineInput {
  /** The version the user was looking at. A mismatch is a 409, never a silent overwrite. */
  version?: number
  status?: 'pending' | 'skipped'
  skip_reason?: string | null
}

/** Add a line by hand. Everything may stay empty and be filled in cell by cell. */
export const addInvoiceLine = (invoiceId: number, data: InvoiceLineInput = {}) =>
  apiRequest<InvoiceLine>(`/api/invoices/${invoiceId}/lines`, { method: 'POST', data: { ...data } })

/**
 * Change one draft line. Returns the stored line, including its new `version`.
 *
 * Only the keys present are touched — the backend's `field()` helper treats an
 * absent key as "keep" and an explicit `null` as "set to unknown", so a partial
 * save must not spell out fields the user did not edit.
 */
export const updateInvoiceLine = (invoiceId: number, lineId: number, data: InvoiceLinePatch) =>
  apiRequest<InvoiceLine>(`/api/invoices/${invoiceId}/lines/${lineId}`, { method: 'PATCH', data: { ...data } })

/** Delete a line. The backend allows this only for hand-added lines (`raw_fields` NULL). */
export const deleteInvoiceLine = (invoiceId: number, lineId: number) =>
  apiRequest<{ status: string }>(`/api/invoices/${invoiceId}/lines/${lineId}`, { method: 'DELETE' })

/**
 * "Which food item did this supplier description get confirmed to before?"
 *
 * Exact hit on the normalised description. ⚠️ Useless on handwriting: the
 * cross-model disagreement on Chinese product names was 96.0%, so the text
 * itself is near-certainly wrong there — that path uses `supplier-candidates`.
 */
export const matchDescription = (description: string, source?: string | null) => {
  const params = new URLSearchParams({ description })
  if (source) params.set('source', source)
  return apiRequest<MatchCandidateList>(`/api/invoices/match?${params.toString()}`)
}

/** What this supplier has been bought from before, most recently used first. */
export const getSupplierCandidates = (invoiceId: number, source?: string | null) => {
  const params = new URLSearchParams()
  if (source) params.set('source', source)
  const query = params.toString()
  return apiRequest<MatchCandidateList>(
    `/api/invoices/${invoiceId}/supplier-candidates${query ? `?${query}` : ''}`
  )
}

// --- F08：逐行状态与入账 ---------------------------------------------------
export interface ConfirmLineChoice {
  line_id: number
  version: number
  food_item_id?: number | null
  new_food_item?: { name: string; category_id?: number; use_purchase_unit_as_base: true }
  accept_amount_mismatch?: boolean
}

export interface ConfirmLineResult {
  line_id: number
  ok: boolean
  purchase_id: number | null
  error?: { code: string; message: string; details?: Record<string, unknown> }
}

export interface ConfirmOutcome {
  confirmed: number
  failed: number
  results: ConfirmLineResult[]
  invoice: Invoice
}

/** Always selects one line; never sends the backend's `all: true` shortcut. */
export const confirmInvoiceLine = (invoiceId: number, choice: ConfirmLineChoice) =>
  apiRequest<ConfirmOutcome>(`/api/invoices/${invoiceId}/confirm`, {
    method: 'POST', data: { lines: [choice] }
  })

export const unconfirmInvoiceLine = (invoiceId: number, lineId: number, version: number) =>
  apiRequest<{ status: 'unconfirmed'; message: string; line: InvoiceLine }>(
    `/api/invoices/${invoiceId}/lines/${lineId}/unconfirm`,
    { method: 'POST', data: { version } }
  )

// --- F09：整单收口 ---------------------------------------------------------
export const completeInvoice = (invoiceId: number) =>
  apiRequest<{ status: 'completed'; message: string; invoice: Invoice }>(
    `/api/invoices/${invoiceId}/complete`, { method: 'POST' }
  )

export const reopenInvoice = (invoiceId: number) =>
  apiRequest<Invoice>(`/api/invoices/${invoiceId}/reopen`, { method: 'POST' })

// --- F11：表格预览、创建与采购明细下载 --------------------------------------
export interface ImportIssue {
  field: string
  code: string
  message: string
  sheet?: string
  row?: number
  column?: string | null
  cell?: string | null
}
export interface ImportPreview {
  filename: string
  sheets: string[]
  import_metadata: {
    sheet: string
    mapping: Record<string, number>
    row_count: number
    blank_rows_ignored: number
    notice: string
  }
  defaults: Record<string, string | null>
  rows: Array<{
    source_row: number
    values: Record<string, string | null>
    raw_fields: Record<string, unknown>
    review_issues: ImportIssue[]
  }>
}
export type ImportOptions = Record<string, unknown>
export const exportSelectedInvoices = (format: 'csv' | 'xlsx', invoiceIds: number[]) =>
  apiRequestBinary('/api/invoices/export', {
    method: 'POST', data: { format, invoice_ids: invoiceIds },
    headers: { 'Content-Type': 'application/json' },
  })

// --- 价格历史与基础单位目录 ------------------------------------------------
export const getPriceUnits = () => apiRequest<PriceUnits>('/api/prices/units')
export const getPriceHistory = (foodItemId: number, currency: string | null, offset = 0) => {
  const params = new URLSearchParams({ limit: '20', offset: String(offset) })
  if (currency) params.set('currency', currency)
  return apiRequest<PriceHistory>(`/api/prices/food-items/${foodItemId}/history?${params}`)
}
// --- 价格趋势、来源与提醒 -----------------------------------------------
export const getWeeklyPrices = (foodItemId: number, currency: string | null, weeks = 6) => {
  const params = new URLSearchParams({ weeks: String(weeks) })
  if (currency) params.set('currency', currency)
  return apiRequest<WeeklyPrices>(`/api/prices/food-items/${foodItemId}/weekly?${params}`)
}
export const getMonthlyPrices = (foodItemId: number, currency: string | null, months = 6) => {
  const params = new URLSearchParams({ months: String(months) })
  if (currency) params.set('currency', currency)
  return apiRequest<MonthlyPrices>(`/api/prices/food-items/${foodItemId}/monthly?${params}`)
}
export const getSourcePrices = (foodItemId: number, currency: string | null, period: 'week' | 'month' = 'week') => {
  const params = new URLSearchParams({ period })
  if (currency) params.set('currency', currency)
  return apiRequest<SourcePrices>(`/api/prices/food-items/${foodItemId}/sources?${params}`)
}
export const getPriceAlerts = (threshold: string | null = null, period: 'week' | 'month' = 'week') => {
  const params = new URLSearchParams({ period })
  if (threshold !== null) params.set('threshold', threshold)
  return apiRequest<PriceAlerts>(`/api/prices/alerts?${params}`)
}
export const getAlertSettings = () => apiRequest<AlertSettings>('/api/prices/alert-settings')
export const putAlertSettings = (data: { enabled: boolean; threshold_percent: string }) =>
  apiRequest<AlertSettings>('/api/prices/alert-settings', { method: 'PUT', data })
