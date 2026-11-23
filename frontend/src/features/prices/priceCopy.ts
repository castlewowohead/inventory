import type { PriceExclusions } from '../../shared/api'

export function exclusionMessages(excluded: PriceExclusions): string[] {
  const messages: string[] = []
  if (excluded.undated) messages.push(`${excluded.undated} 笔采购未填写日期，无法计入价格统计。`)
  if (excluded.unit_unknown) messages.push(`${excluded.unit_unknown} 笔采购的单位无法识别，暂不能比较单价。`)
  if (excluded.pending_pack_spec) messages.push(`${excluded.pending_pack_spec} 笔采购的单位需要统一，暂不能比较单价。`)
  if (excluded.incompatible_unit) messages.push(`${excluded.incompatible_unit} 笔采购的单位与食材单位不一致，暂不能比较单价。`)
  if (excluded.other_currency) messages.push(`${excluded.other_currency} 笔采购使用其他币种，请分开查看。`)
  return messages
}
