export const cny = (n: number) =>
  '¥' + n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export const int = (n: number) => n.toLocaleString('zh-CN')

export const pct = (n: number) => `${n > 0 ? '+' : ''}${n.toFixed(1)}%`

export const signed = (n: number) =>
  `${n > 0 ? '+' : ''}${n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`

export function relTime(iso: string | null): string {
  if (!iso) return '从未'
  // 后端发的是不带时区的 UTC 时间，按 UTC 解析，避免被当成本地时间差 8 小时
  const norm = /[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`
  const t = new Date(norm).getTime()
  const diff = Date.now() - t
  if (diff < 0) return '刚刚'
  const m = Math.floor(diff / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  const d = Math.floor(h / 24)
  return `${d} 天前`
}

