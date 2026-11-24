export interface ApiErrorBody {
  code: string
  message: string
  details?: Record<string, unknown>
}

export class ApiError extends Error {
  readonly code: string
  readonly status: number | null
  readonly details?: Record<string, unknown>

  constructor(body: ApiErrorBody, status: number | null = null) {
    super(body.message)
    this.name = 'ApiError'
    this.code = body.code
    this.status = status
    this.details = body.details
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const fieldNames: Record<string, string> = {
  purchase_date: '采购日期', invoice_date: '单据日期', source_name: '来源',
  invoice_number: '单号', invoice_total: '单据总额', quantity: '数量',
  purchase_unit: '采购单位', unit_price: '单价', line_amount: '金额',
  food_item_id: '食材', category_id: '分类', base_unit: '基础单位',
  name_zh: '中文名', line_kind: '记录类型', currency: '币种',
  date_from: '开始日期', date_to: '结束日期', header_row: '表头所在行',
  threshold_percent: '提醒涨幅', source_ids: '所选食材',
  confirmed_line_ids: '已入账明细', pack_size: '包装数量', pack_unit: '包装单位'
}

/** The API may return useful validation details with internal field names. */
export function userMessage(message: string): string {
  const translated = message
    .replace(/\b(?:purchase_date|invoice_date|source_name|invoice_number|invoice_total|quantity|purchase_unit|unit_price|line_amount|food_item_id|category_id|base_unit|name_zh|line_kind|currency|date_from|date_to|header_row|threshold_percent|source_ids|confirmed_line_ids|pack_size|pack_unit)\b/g, (key) => fieldNames[key])
    .replace(/\bnull\b/g, '留空')
  if (/\/api\/|\b(?:GET|POST|PATCH|DELETE|HTTP|Flask|JSON)\b|\b[a-z]+_[a-z_]+\b/.test(translated)) {
    return '暂时无法完成操作，请检查填写的内容后重试。'
  }
  return translated
}

export function responseError(status: number, data: unknown): ApiError {
  const body = record(data) && record(data.error) ? data.error : null
  if (body && typeof body.code === 'string' && typeof body.message === 'string') {
    return new ApiError({
      code: body.code,
      message: userMessage(body.message),
      details: record(body.details) ? body.details : undefined
    }, status)
  }
  return new ApiError({
    code: 'http_error',
    message: status >= 500
      ? '暂时无法处理，请稍后重试。'
      : '操作未成功，请检查填写的内容后重试。'
  }, status)
}

export function networkError(reason: unknown): ApiError {
  const detail = record(reason) && typeof reason.errMsg === 'string' ? reason.errMsg : undefined
  return new ApiError({
    code: 'network_error',
    message: '暂时无法连接，请检查网络后重试。',
    details: detail ? { cause: detail } : undefined
  })
}

export function errorMessage(reason: unknown): string {
  if (reason instanceof Error && reason.message) return userMessage(reason.message)
  return '请求失败，请稍后重试。'
}
