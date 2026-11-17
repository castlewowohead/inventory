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

export function responseError(status: number, data: unknown): ApiError {
  const body = record(data) && record(data.error) ? data.error : null
  if (body && typeof body.code === 'string' && typeof body.message === 'string') {
    return new ApiError({
      code: body.code,
      message: body.message,
      details: record(body.details) ? body.details : undefined
    }, status)
  }
  return new ApiError({
    code: 'http_error',
    message: status >= 500
      ? `服务暂时不可用（HTTP ${status}），请检查 Flask 是否已在 127.0.0.1:5000 启动。`
      : `请求失败（HTTP ${status}），请稍后重试。`
  }, status)
}

export function networkError(reason: unknown): ApiError {
  const detail = record(reason) && typeof reason.errMsg === 'string' ? reason.errMsg : undefined
  return new ApiError({
    code: 'network_error',
    message: '无法连接后端服务，请检查 Flask 是否已在 127.0.0.1:5000 启动。',
    details: detail ? { cause: detail } : undefined
  })
}

export function errorMessage(reason: unknown): string {
  if (reason instanceof Error && reason.message) return reason.message
  return '请求失败，请稍后重试。'
}
