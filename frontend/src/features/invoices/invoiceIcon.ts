import type { Invoice } from '../../shared/api'

export type InvoiceIconKind = 'table' | 'pdf' | 'photo' | 'other'

export function invoiceIconKind(invoice: Invoice): InvoiceIconKind {
  const source = invoice.intake_sources[0]
  const file = invoice.files[0]
  const name = source?.original_name ?? file?.original_name ?? ''
  const extension = name.split('.').pop()?.toLowerCase() ?? ''
  const mediaType = source?.content_type ?? file?.content_type ?? ''
  if (source?.kind === 'row' || invoice.entry_source === 'import' || ['csv', 'xlsx', 'xls'].includes(extension)) return 'table'
  if (extension === 'pdf' || mediaType === 'application/pdf') return 'pdf'
  if (['jpg', 'jpeg', 'png', 'heic', 'heif'].includes(extension) || mediaType.startsWith('image/')) return 'photo'
  return 'other'
}

export function invoiceIconLabel(invoice: Invoice): string {
  const name = invoice.intake_sources[0]?.original_name ?? invoice.files[0]?.original_name ?? ''
  const extension = name.split('.').pop()?.toLowerCase() ?? ''
  const kind = invoiceIconKind(invoice)
  if (kind === 'table') return extension === 'csv' ? 'CSV' : 'EXCEL'
  if (kind === 'pdf') return 'PDF'
  if (kind === 'photo') return extension === 'png' ? 'PNG' : ['heic', 'heif'].includes(extension) ? 'HEIC' : 'JPG'
  return '文件'
}
