/** Wire types mirror backend/src/api/schemas.py. Decimal values stay as strings. */
export type DecimalText = string | null

export interface IntakeBatchFile {
  id: number
  original_name: string
  status: 'pending' | 'ready' | 'failed' | 'discarded'
  item_count: number | null
  /** How many of this file's pages are already in a draft — blocks removing it. */
  handed_off_item_count: number
  /** When this file entered the batch. Two files can share a name; this tells them apart. */
  created_at: string
  size: number
  sheet_name: string | null
  error: { code: string; message: string } | null
}

export interface IntakeBatch {
  id: number
  status: 'open' | 'closed' | 'discarded'
  files: IntakeBatchFile[]
  /** How many classifiable contents this batch holds right now. */
  item_count: number
  /**
   * How many of them are not in a completed invoice yet (plan section 22).
   *
   * The board only loads the groups that are open, so counting the cards on
   * screen would miss whatever sits inside a collapsed one — the same reason
   * 「重试识别失败项」 asks the server (section 18 ①).
   */
  unfinished_item_count: number
  created_at: string
  updated_at: string
}

/** One classification suggestion. Never a decision: M06 only writes `decided_*`. */
export interface IntakeCandidate {
  id: number
  field: 'purchase_date' | 'source_name'
  value: string | null
  value_normalized: string | null
  basis: 'paper' | 'same_file' | 'history' | 'similar_layout'
  confidence: DecimalText
  evidence: Record<string, unknown> | null
  created_at: string
}

/** How far date/supplier recognition got on one content. `status: null` = never run. */
export interface IntakeRecognition {
  status: 'pending' | 'done' | 'failed' | null
  recognized_at: string | null
  provider: string | null
  models: string[]
  prompt_sha12: string | null
  is_fake: boolean | null
  date_raw_text: string | null
  date_ambiguous: boolean
  /** Set when the day/month order was settled by the project default, not by the paper. */
  date_assumed_order: string | null
  date_problem: string | null
  date_readings: { value: string; order: string | null; note: string | null }[]
  date_order: string | null
  date_order_from: string | null
  notes: string[]
  error: { code: string; message: string } | null
}

/** One classifiable content: a PDF/image page or a spreadsheet row. */
export interface IntakeItem {
  id: number
  batch_id: number
  batch_file_id: number
  kind: 'page' | 'row'
  page_no: number | null
  row_no: number | null
  sheet_name: string | null
  original_name: string
  sha256: string
  decided_purchase_date: string | null
  decided_source_name: string | null
  decided_source_name_normalized: string | null
  decided_at: string | null
  version: number
  /**
   * Set once M08 handed this content to review. Non-null means the organizer
   * must not try to move it: the server answers 409, and the date/supplier are
   * changed through the review's classification revision instead.
   */
  handed_off_at: string | null
  /**
   * The draft this content is being filled in, while `handed_off_at` is set.
   * Without it the organizer has no way back into the fill page once the user
   * leaves it (`docs/multifiles.md` 第 11 节 ④).
   */
  review_invoice_id: number | null
  /** Where that draft stands: still being filled, booked, or discarded. */
  review_status: 'draft' | 'completed' | 'voided' | null
  pile_excluded: boolean
  /** Parsed spreadsheet cells; decimal fields remain strings from the server. */
  row_values: Record<string, string | null> | null
  candidates: IntakeCandidate[]
  recognition: IntakeRecognition
}

/**
 * A derived (date, supplier) content group. Empty date headers are persisted
 * separately, so a date remains visible after its last content moves away.
 */
export interface IntakeGroup {
  purchase_date: string | null
  source_name: string | null
  source_name_normalized: string | null
  item_count: number
  suggested_count: number
}

export interface IntakeGroupPage {
  groups: IntakeGroup[]
  total: number
  limit: number
  offset: number
}

/** The result of one move command, or of undoing it. */
export interface IntakeMoveResult {
  move_id: number
  request_key: string
  undone: boolean
  items: IntakeItem[]
}

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface Health {
  status: string
  app_env: string
  version: string
}

export interface Category {
  id: number
  name: string
  name_zh: string | null
  created_at: string
  updated_at: string
}

export interface CategoryRef {
  id: number
  name: string
  name_zh: string | null
}

export interface FoodItemRef {
  id: number
  name: string
  name_zh: string | null
  specification: string | null
  base_unit: string
  category: CategoryRef
  needs_review: boolean
}

export interface FoodItem extends FoodItemRef {
  created_at: string
  updated_at: string
}

export interface MergePlan {
  target: FoodItem
  sources: Array<{
    food_item: FoodItem
    purchase_count: number
    invoice_line_count: number
    pack_spec_count: number
    duplicate_pack_spec_count: number
  }>
  can_merge: boolean
  blockers: Array<{ code: string; message: string; details: Record<string, unknown> }>
  moved_purchase_count: number
  moved_invoice_line_count: number
  moved_pack_spec_count: number
  notes: string[]
}

export interface FoodItemSuggestion {
  food_item: FoodItem
  match_reason: 'exact' | 'prefix' | 'contains' | 'fuzzy' | 'merged_from' | 'recent' | 'frequent'
  score: number | null
  purchase_count: number | null
  last_purchase_date: string | null
  merged_from: string | null
}

export interface FoodItemSuggestions {
  query: string | null
  matches: FoodItemSuggestion[]
  recent: FoodItemSuggestion[]
  frequent: FoodItemSuggestion[]
  notes: string[]
}

export interface InvoiceFile {
  id: number
  original_name: string
  size: number
  sha256: string
  content_type: string
  page_order: number
  created_at: string
}

export interface IntakeSource {
  batch_id: number
  item_id: number
  batch_file_id: number
  version: number
  kind: 'page' | 'row'
  page_no: number | null
  row_no: number | null
  original_name: string
  content_type: string | null
}

export interface MatchCandidate {
  food_item: FoodItemRef
  purchase_count: number
  last_purchase_id: number | null
  last_purchase_date: string | null
  same_source: boolean
}

/**
 * One unresolved question the backend recorded about a draft line.
 *
 * ⚠️ These are **objects, not strings**: `review_issues` is handed over from
 * `invoice_lines.review_metadata["issues"]`, written either by the extraction
 * (`field` / `code` / `raw_text` / `message`) or by the spreadsheet import
 * (which adds `sheet` / `row` / `column` / `cell`). A line that still carries
 * any of them cannot be confirmed — `confirm_lines` rejects it with a 400 —
 * and the backend drops one only when its own `field` is PATCHed.
 */
export interface ReviewIssue {
  field: string
  code?: string
  message?: string
  raw_text?: string | null
  sheet?: string | null
  row?: number | null
  column?: number | null
  cell?: string | null
}

export interface InvoiceLine {
  id: number
  line_no: number
  status: 'pending' | 'confirmed' | 'skipped'
  skip_reason: string | null
  line_kind: 'purchase' | 'adjustment'
  food_item: FoodItemRef | null
  description: string | null
  quantity: DecimalText
  purchase_unit: string | null
  unit_price: DecimalText
  line_amount: DecimalText
  purchase_date: string | null
  source_name: string | null
  currency: string | null
  raw_fields: Record<string, unknown>
  review_issues: ReviewIssue[]
  page: number | null
  version: number
  confirmed_at: string | null
  purchase_id: number | null
  missing_fields: string[]
  match_candidates: MatchCandidate[]
  food_item_match: {
    reason: string
    food_item_id?: number
    keyword?: string | null
    description?: string | null
    suggested_name?: string
    suggested_name_zh?: string | null
    suggested_category_id?: number | null
    unit_raw?: string | null
  } | null
  created_at: string
  updated_at: string
}

export interface CategoryTotal {
  category_id: number | null
  name: string
  name_zh: string | null
  line_count: number
  amount: string
}

export interface Reconciliation {
  status: string
  invoice_total: DecimalText
  confirmed_amount: DecimalText
  difference: DecimalText
  tolerance: string
  line_count: number
  pending_count: number
  confirmed_count: number
  skipped_count: number
  by_category: CategoryTotal[]
  adjustment_total: CategoryTotal | null
  notes: string[]
}

export interface Invoice {
  id: number
  status: 'draft' | 'in_review' | 'ready_to_complete' | 'completed' | 'voided'
  source_name: string | null
  invoice_number: string | null
  invoice_date: string | null
  currency: string | null
  invoice_total: DecimalText
  header_reviewed_fields: string[]
  recognition: {
    provider: string | null
    is_fake: boolean
    recognized_at: string | null
    raw_document_fields: Record<string, unknown>
  }
  uploaded_at: string
  completed_at: string | null
  voided_at: string | null
  updated_at: string
  files: InvoiceFile[]
  intake_sources: IntakeSource[]
  lines: InvoiceLine[]
  reconciliation: Reconciliation
  possible_duplicate_invoice_ids: number[]
  entry_source: string
  import_metadata: Record<string, unknown> | null
}

/** Current booked record; a completed invoice line may be older than this record. */
export interface Purchase {
  id: number
  line_kind: 'purchase' | 'adjustment'
  food_item: FoodItemRef | null
  purchase_date: string | null
  source_name: string | null
  original_description: string | null
  quantity: DecimalText
  purchase_unit: string | null
  purchase_unit_normalized: string | null
  unit_price: DecimalText
  line_amount: DecimalText
  currency: string | null
  amount_mismatch: boolean
  entry_source: string
  source_invoice: { invoice_id: number; invoice_line_id: number; line_no: number } | null
  voided_at: string | null
  revision_count: number
  created_at: string
  updated_at: string
}

/** One extraction job (backend step 08). Mirrors `_job_payload` in backend/src/api/invoices.py. */
export interface ExtractionJob {
  id: number
  invoice_id: number
  invoice_file_id: number | null
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  attempt: number
  doc_class: string | null
  route_source: string | null
  provider: string | null
  models: string[]
  prompt_sha12: string | null
  pages_total: number | null
  pages_done: number
  lines_created: number
  error_code: string | null
  error_message: string | null
  warnings: string[]
  usage: Record<string, unknown>
  started_at: string | null
  finished_at: string | null
  created_at: string | null
}

export interface ItemList<T> {
  items: T[]
  total: number
}

export interface PriceUnits {
  volume_system: string
  dimensions: Array<{ name: string; units: Array<{ unit: string; per_reference_unit: string }> }>
  packaging_units: string[]
  rules: string[]
}

export interface PackSpec {
  id: number
  food_item_id: number
  food_item?: FoodItemRef
  source_name: string | null
  source_name_normalized: string | null
  purchase_unit: string
  pack_size: string
  pack_unit: string
  spec_text: string | null
  note: string | null
  created_at: string
  updated_at: string
}

export interface PriceHistory {
  food_item: FoodItemRef
  base_unit: string
  currency: string | null
  items: Array<{
    purchase: Purchase
    conversion: {
      status: 'available' | 'pending_pack_spec' | 'unit_unknown' | 'incompatible_unit'
      base_unit: string
      base_quantity: string | null
      normalized_unit_price: string | null
      pack_spec: PackSpec | null
      pack_spec_match: string | null
      reason: string | null
    }
  }>
  total: number
  limit: number
  offset: number
  convertible_count: number
  excluded: {
    total: number
    undated: number
    unit_unknown: number
    pending_pack_spec: number
    incompatible_unit: number
    other_currency: number
  }
  available_currencies: string[]
  notes: string[]
}

export interface PriceExclusions {
  total: number
  undated: number
  unit_unknown: number
  pending_pack_spec: number
  incompatible_unit: number
  other_currency: number
}

export interface PricePeriod {
  key: string
  date_from: string
  date_to: string
  days: number
  basis: string
}

export interface PricePeriodStats {
  period: PricePeriod
  weighted_average_unit_price: string | null
  total_amount: string
  total_base_quantity: string
  purchase_count: number
  zero_amount_count: number
  amount_mismatch_count: number
  purchase_ids: number[]
}

export interface PriceComparison {
  status: 'available' | 'no_base_data' | 'no_current_data' | 'base_price_is_zero'
  reason: string | null
  current: PricePeriodStats
  base: PricePeriodStats
  change_amount: string | null
  change_percent: string | null
}

export interface MonthlyPrices {
  food_item: FoodItemRef
  currency: string
  base_unit: string
  months: PricePeriodStats[]
  comparison: PriceComparison
  excluded: PriceExclusions
  available_currencies: string[]
  notes: string[]
}

export interface WeeklyPrices {
  food_item: FoodItemRef
  currency: string
  base_unit: string
  weeks: PricePeriodStats[]
  comparison: PriceComparison
  excluded: PriceExclusions
  available_currencies: string[]
  notes: string[]
}

export interface SourcePrices {
  food_item: FoodItemRef
  currency: string
  base_unit: string
  period: PricePeriod
  sources: Array<{
    source_name: string | null
    source_name_normalized: string | null
    weighted_average_unit_price: string
    total_amount: string
    total_base_quantity: string
    purchase_count: number
    first_purchase_date: string
    latest_purchase_date: string
    descriptions: string[]
    pack_spec: PackSpec | null
  }>
  cheapest_source_normalized: string | null
  spread_amount: string | null
  spread_percent: string | null
  excluded: PriceExclusions
  available_currencies: string[]
  notes: string[]
}

export interface AlertSettings {
  enabled: boolean
  threshold_percent: string
  updated_at: string
}

export interface PriceAlerts {
  settings: AlertSettings
  threshold_percent: string
  current_period: PricePeriod
  base_period: PricePeriod
  alerts: Array<{ food_item: FoodItemRef; currency: string; base_unit: string; comparison: PriceComparison }>
  compared_count: number
  skipped: Record<string, number>
  excluded: PriceExclusions
  notes: string[]
}
