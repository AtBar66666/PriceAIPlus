import { useState } from 'react'
import { ExternalLink, LoaderCircle, RefreshCw, Store, X } from 'lucide-react'
import { api, type Product } from '../lib/api'
import { BrandIcon } from '../components/BrandIcon'
import { StatusTag, StockPill } from '../components/ui'
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
    return parsed.hostname.toLowerCase() === 'pay.ldxp.cn' && /^\/item\/[^/]+\/?$/i.test(parsed.pathname)
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
    { label: '售价', value: `¥${current.sale_price.toFixed(2)}` },
    { label: '库存', value: stockText },
    { label: '平台', value: platform },
    { label: '品类', value: current.category },
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
      toast(`已查最新：${latest.stock < 0 ? '库存未知' : `库存 ${latest.stock.toLocaleString('zh-CN')}`}`, 'success')
    } catch {
      toast('原店查询失败，本地结果未改动', 'error')
    } finally {
      setChecking(false)
    }
  }

  return (
    <div className="drawer-overlay fixed inset-0 z-[60] flex justify-end bg-[var(--overlay)] backdrop-blur-sm" onClick={onClose}>
      <div
        className="drawer-panel flex h-full w-[min(620px,95vw)] flex-col bg-white shadow-[var(--shadow-float)]"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-start gap-4 border-b border-[var(--border-subtle)] p-7">
          <BrandIcon name={current.name} category={current.category} />
          <div className="min-w-0 flex-1">
            <div className="mb-2.5 flex flex-wrap items-center gap-1.5">
              <StockPill stock={current.stock} status={current.status} />
              {current.is_linked && <StatusTag label="已对接" tone="ok" />}
            </div>
            <h2 className="text-[21px] font-bold leading-snug text-[var(--ink)]">{current.name}</h2>
            <div className="mt-2 text-[13px] text-[var(--soft)]">
              {current.merchant_name || '未知商家'} · 更新于 {relTime(current.last_seen_at)}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭商品详情"
            className="ui-control group inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-transparent text-[var(--soft)] hover:bg-[var(--brand-soft)] hover:text-[var(--success-text)]"
          >
            <X size={19} className="transition-transform duration-200 group-hover:scale-110" />
          </button>
        </div>

        <div className="grid grid-cols-2 border-b border-[var(--border-subtle)] sm:grid-cols-4">
          {cells.map((cell) => (
            <div key={cell.label} className="border-r border-[var(--border-subtle)] px-6 py-5 last:border-r-0">
              <div className="t-label">{cell.label}</div>
              <div className="mt-2 truncate text-[17px] font-bold text-[var(--ink)]">{cell.value}</div>
            </div>
          ))}
        </div>

        <div className="flex-1 overflow-auto p-7">
          <dl className="divide-y divide-[var(--border-subtle)] border-y border-[var(--border-subtle)]">
            <div className="flex items-center justify-between gap-6 py-4">
              <dt className="text-[13px] text-[var(--soft)]">商家</dt>
              <dd className="text-right text-[14px] font-medium text-[var(--ink)]">{current.merchant_name || '-'}</dd>
            </div>
            <div className="flex items-center justify-between gap-6 py-4">
              <dt className="text-[13px] text-[var(--soft)]">代理价</dt>
              <dd className="num text-[14px] font-medium text-[var(--ink)]">
                {current.agent_price > 0 ? `¥${current.agent_price.toFixed(2)}` : '-'}
              </dd>
            </div>
            <div className="flex items-center justify-between gap-6 py-4">
              <dt className="text-[13px] text-[var(--soft)]">成本价</dt>
              <dd className="num text-[14px] font-medium text-[var(--ink)]">
                {current.cost_price > 0 ? `¥${current.cost_price.toFixed(2)}` : '-'}
              </dd>
            </div>
          </dl>

          {canCheckOrigin(current.url) && (
            <div className="mt-7 rounded-2xl border border-[var(--border-subtle)] bg-[var(--panel-soft)] p-4">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <div className="text-[13.5px] font-semibold text-[var(--ink)]">需要最新库存？</div>
                  <div className="mt-1 text-[12px] leading-5 text-[var(--soft)]">普通搜索会核对当前页；这里可单独重查这一件。</div>
                </div>
                <button
                  type="button"
                  disabled={checking}
                  onClick={() => void checkLatest()}
                  className="ui-control inline-flex h-10 shrink-0 items-center gap-2 rounded-full border border-[var(--border)] bg-white px-4 text-[13px] font-semibold text-[var(--success-text)] shadow-[var(--shadow-xs)] hover:bg-[var(--brand-soft)] disabled:pointer-events-none disabled:opacity-55"
                >
                  {checking ? <LoaderCircle size={15} className="animate-spin" /> : <RefreshCw size={15} />}
                  {checking ? '查询中' : '手动查最新'}
                </button>
              </div>
            </div>
          )}

          {(current.url || current.shop_url) && (
            <div className={`mt-7 grid gap-3 ${current.shop_url ? 'grid-cols-2' : 'grid-cols-1'}`}>
              {current.url && (
                <button
                  type="button"
                  onClick={() => {
                    void openExternal(current.url).catch(() => toast('无法打开商品链接', 'error'))
                  }}
                  className="ui-control group inline-flex h-12 items-center justify-center gap-2 rounded-full bg-[var(--primary)] text-[15px] font-medium text-[var(--on-primary)] shadow-[var(--shadow-pop)] hover:bg-[var(--primary-hover)]"
                >
                  <ExternalLink size={17} className="transition-transform duration-200 group-hover:translate-x-0.5 group-hover:-translate-y-0.5" />
                  打开商品页
                </button>
              )}
              {current.shop_url && (
                <button
                  type="button"
                  onClick={() => {
                    void openExternal(current.shop_url).catch(() => toast('无法打开零售店铺', 'error'))
                  }}
                  className="ui-control group inline-flex h-12 items-center justify-center gap-2 rounded-full border border-[var(--border)] bg-white text-[15px] font-medium text-[var(--ink)] shadow-[var(--shadow-xs)] hover:border-[color-mix(in_srgb,var(--brand)_45%,var(--border))] hover:bg-[var(--brand-soft)] hover:text-[var(--success-text)] hover:shadow-[var(--shadow-hover)]"
                >
                  <Store size={17} className="transition-transform duration-200 group-hover:scale-105" />
                  进入零售店
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
