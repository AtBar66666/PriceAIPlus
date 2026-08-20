import { useState } from 'react'
import { ExternalLink, LoaderCircle, RefreshCw, Store, X } from 'lucide-react'
import { api, type Product } from '../lib/api'
import { BrandIcon } from '../components/BrandIcon'
import { Button, IconButton, StatusText, StockCell } from '../components/ui'
import { relTime } from '../lib/format'
import { openExternal } from '../lib/openExternal'
import { toast } from '../lib/toast'

function platformLabel(name: string): string | null {
  if (/claude|anthropic|sonnet|\bopus\b|haiku/i.test(name)) return 'Claude'
  if (/gemini|谷歌|google|bard/i.test(name)) return 'Gemini'
  if (/grok/i.test(name)) return 'Grok'
  if (/gpt|chatgpt|openai|codex|sub2api|k12|bug\s*team|team\s*bug/i.test(name)) return 'OpenAI'
  if (/邮箱|mail|icloud|outlook|gmail|hotmail|@/i.test(name)) return '邮箱'
  return null
}

function canCheckOrigin(url: string): boolean {
  try {
    const parsed = new URL(url)
    return (
      parsed.hostname.toLowerCase() === 'pay.ldxp.cn' && /^\/item\/[^/]+\/?$/i.test(parsed.pathname)
    )
  } catch {
    return false
  }
}

export function ProductDrawer({ product, onClose }: { product: Product; onClose: () => void }) {
  const [current, setCurrent] = useState(product)
  const [checking, setChecking] = useState(false)
  const stockText =
    current.status === '未上架'
      ? '未上架'
      : current.stock < 0
        ? '库存未知'
        : current.stock.toLocaleString('zh-CN')
  const platform = platformLabel(current.name) ?? current.category
  const cells = [
    { label: '售价', value: `¥${current.sale_price.toFixed(2)}`, num: true },
    { label: '库存', value: stockText, num: true },
    { label: '平台', value: platform, num: false },
    { label: '品类', value: current.category, num: false },
  ]

  const checkLatest = async () => {
    if (!canCheckOrigin(current.url) || checking) return
    setChecking(true)
    try {
      const response = await api.liveSearch({
        keywords: current.url,
        in_stock: false,
        page: 1,
        page_size: 1,
        platform: 'ldxp',
      })
      const latest = response.items[0]
      if (!latest) {
        setCurrent((value) => ({
          ...value,
          status: '未上架',
          stock: 0,
          verified: true,
          verified_at: new Date().toISOString(),
        }))
        toast('原店已下架或商品不存在', 'error')
        return
      }
      setCurrent(latest)
      toast(
        `已查最新：${latest.stock < 0 ? '库存未知' : `库存 ${latest.stock.toLocaleString('zh-CN')}`}`,
        'success',
      )
    } catch {
      toast('原店查询失败，本地结果未改动', 'error')
    } finally {
      setChecking(false)
    }
  }

  return (
    <div
      className="drawer-overlay fixed inset-0 z-[60] flex justify-end bg-[var(--overlay)]"
      onClick={onClose}
    >
      <div
        className="drawer-panel flex h-full w-[min(560px,95vw)] flex-col border-l border-[var(--border)] bg-[var(--card)] shadow-[var(--shadow-float)]"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-start gap-3 border-b border-[var(--border-soft)] p-6">
          <BrandIcon name={current.name} category={current.category} />
          <div className="min-w-0 flex-1">
            <h2 className="text-[17px] font-bold leading-[1.45] tracking-[-0.01em] text-[var(--ink)]">
              {current.name}
            </h2>
            <div className="mt-1.5 text-[13.5px] text-[var(--muted)]">
              {current.merchant_name || '未知商家'} · 更新于 {relTime(current.last_seen_at)}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-3">
              <StockCell stock={current.stock} status={current.status} />
              {current.is_linked && <StatusText label="已对接" tone="ok" />}
            </div>
          </div>
          <IconButton type="button" onClick={onClose} aria-label="关闭商品详情">
            <X size={16} />
          </IconButton>
        </div>

        <div className="grid grid-cols-2 divide-x divide-[var(--border-soft)] border-b border-[var(--border)] sm:grid-cols-4">
          {cells.map((cell) => (
            <div key={cell.label} className="min-w-0 px-4 py-4 first:pl-6">
              <div className="t-label truncate">{cell.label}</div>
              <div
                className={
                  cell.num
                    ? 'num-lg mt-1.5 truncate text-[19px] text-[var(--ink)]'
                    : 'mt-1.5 truncate text-[15.5px] font-bold leading-[1.2] text-[var(--ink)]'
                }
              >
                {cell.value}
              </div>
            </div>
          ))}
        </div>

        <div className="flex-1 overflow-auto p-6">
          <dl className="space-y-3">
            <div className="flex items-baseline justify-between gap-6 text-[13px]">
              <dt className="text-[var(--muted)]">商家</dt>
              <dd className="truncate font-medium text-[var(--ink)]">
                {current.merchant_name || '-'}
              </dd>
            </div>
            <div className="flex items-baseline justify-between gap-6 text-[13px]">
              <dt className="text-[var(--muted)]">代理价</dt>
              <dd className="num text-[13.5px] font-semibold text-[var(--ink)]">
                {current.agent_price > 0 ? `¥${current.agent_price.toFixed(2)}` : '-'}
              </dd>
            </div>
            <div className="flex items-baseline justify-between gap-6 text-[13px]">
              <dt className="text-[var(--muted)]">成本价</dt>
              <dd className="num text-[13.5px] font-semibold text-[var(--ink)]">
                {current.cost_price > 0 ? `¥${current.cost_price.toFixed(2)}` : '-'}
              </dd>
            </div>
          </dl>

          {canCheckOrigin(current.url) && (
            <div className="mt-6 flex items-center justify-between gap-4 rounded-[10px] border border-[var(--border)] bg-[var(--head)] px-4 py-3">
              <div className="min-w-0">
                <div className="text-[14px] font-medium text-[var(--ink)]">需要最新库存？</div>
                <div className="mt-1 text-[13px] leading-[1.5] text-[var(--muted)]">
                  可单独重查这一件，不影响列表结果。
                </div>
              </div>
              <Button type="button" disabled={checking} onClick={() => void checkLatest()}>
                {checking ? (
                  <LoaderCircle size={13} className="animate-spin" />
                ) : (
                  <RefreshCw size={13} />
                )}
                {checking ? '查询中' : '查最新'}
              </Button>
            </div>
          )}
        </div>

        {(current.url || current.shop_url) && (
          <div
            className={`grid gap-2.5 border-t border-[var(--border-soft)] p-5 ${current.shop_url && current.url ? 'grid-cols-2' : 'grid-cols-1'}`}
          >
            {current.url && (
              <Button
                type="button"
                variant="primary"
                size="lg"
                onClick={() => {
                  void openExternal(current.url).catch(() => toast('无法打开商品链接', 'error'))
                }}
              >
                <ExternalLink size={14} />
                打开商品页
              </Button>
            )}
            {current.shop_url && (
              <Button
                type="button"
                size="lg"
                onClick={() => {
                  void openExternal(current.shop_url).catch(() =>
                    toast('无法打开零售店铺', 'error'),
                  )
                }}
              >
                <Store size={14} />
                进入零售店
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
