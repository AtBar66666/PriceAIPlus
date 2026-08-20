import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { ExternalLink, LoaderCircle, Plus, RefreshCw, Search, Trash2 } from 'lucide-react'
import { api, type RetailShop } from '../lib/api'
import { relTime } from '../lib/format'
import { openExternal } from '../lib/openExternal'
import { toast } from '../lib/toast'
import {
  Button,
  EmptyState,
  fieldClass,
  IconButton,
  PageHeader,
  Spinner,
  StatusText,
} from '../components/ui'

const GRID = 'grid-cols-[minmax(240px,1fr)_100px_110px_112px]'

function shopInitial(name: string): string {
  const m = name.trim().match(/[A-Za-z0-9\u4e00-\u9fa5]/)
  return m ? m[0].toUpperCase() : '#'
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

function normalizeShopUrl(value: string): string {
  try {
    const parsed = new URL(value.trim())
    parsed.hash = ''
    parsed.search = ''
    return parsed.href.replace(/\/+$/, '').toLowerCase()
  } catch {
    return value.trim().replace(/\/+$/, '').toLowerCase()
  }
}

export function RetailShops() {
  const qc = useQueryClient()
  const [url, setUrl] = useState('')
  const [filter, setFilter] = useState('')
  const [shopPage, setShopPage] = useState(1)

  const shopsQuery = useQuery({
    queryKey: ['retail-shops'],
    queryFn: api.retailShops,
  })
  const shops = shopsQuery.data?.items ?? []

  const invalidateRetailData = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: ['retail-shops'] }),
      qc.invalidateQueries({ queryKey: ['complete-search'] }),
    ])

  const add = useMutation({
    mutationFn: (shopUrl: string) => api.addRetailShop(shopUrl),
    onSuccess: async (result) => {
      setUrl('')
      toast(result.message || '店铺已添加并完成抓取', 'success')
      await invalidateRetailData()
    },
    onError: (error: unknown) => toast(errorMessage(error, '添加店铺失败'), 'error'),
  })

  const refreshOne = useMutation({
    mutationFn: (shopId: number) => api.refreshRetailShop(shopId),
    onSuccess: async (result) => {
      toast(result.message || '店铺刷新完成', 'success')
      await invalidateRetailData()
    },
    onError: (error: unknown) => toast(errorMessage(error, '刷新店铺失败'), 'error'),
  })

  const remove = useMutation({
    mutationFn: (shopId: number) => api.deleteRetailShop(shopId),
    onSuccess: async (result) => {
      toast(result.message || '店铺已删除', 'success')
      await invalidateRetailData()
    },
    onError: (error: unknown) => toast(errorMessage(error, '删除店铺失败'), 'error'),
  })

  const candidate = normalizeShopUrl(url)
  const duplicate =
    url.trim() !== '' &&
    shops.some((shop) => {
      const canonical = normalizeShopUrl(shop.url)
      const tokenUrl = normalizeShopUrl(`https://pay.ldxp.cn/shop/${shop.token}`)
      return candidate === canonical || candidate === tokenUrl
    })
  const syncPending = add.isPending || refreshOne.isPending
  const deletingId = remove.isPending ? remove.variables : undefined
  const refreshingId = refreshOne.isPending ? refreshOne.variables : undefined
  const refreshingShop = shops.find((shop) => shop.id === refreshingId)
  const pendingText = add.isPending
    ? '正在添加店铺并抓取全部商品分页，请稍候'
    : refreshOne.isPending
      ? `正在刷新「${refreshingShop?.name ?? '店铺'}」的全部商品`
      : ''
  const mutationPending = syncPending || remove.isPending
  const pageSize = 50
  const normalizedFilter = filter.trim().toLowerCase()
  const filteredShops = normalizedFilter
    ? shops.filter((shop) =>
        `${shop.name} ${shop.token} ${shop.url}`.toLowerCase().includes(normalizedFilter),
      )
    : shops
  const shopPages = Math.max(1, Math.ceil(filteredShops.length / pageSize))
  const currentShopPage = Math.min(shopPage, shopPages)
  const visibleShops = filteredShops.slice(
    (currentShopPage - 1) * pageSize,
    currentShopPage * pageSize,
  )

  const submit = () => {
    const next = url.trim()
    if (!next || duplicate || mutationPending || shopsQuery.isLoading) return
    add.mutate(next)
  }

  const openShop = (shop: RetailShop) => {
    void openExternal(shop.url).catch((error: unknown) => {
      toast(errorMessage(error, '无法打开店铺链接'), 'error')
    })
  }

  const listError = errorMessage(shopsQuery.error, '店铺列表加载失败')

  return (
    <div className="mx-auto max-w-[960px]">
      <PageHeader
        title="零售店铺"
        desc="这里保存原店 token；实时搜索命中的 PickAI 商品会自动解析并加入，后续直接抓原店。"
        actions={
          <span className="text-[14px] text-[var(--muted)]">已收录 <span className="num font-semibold text-[var(--ink)]">{shops.length.toLocaleString('zh-CN')}</span> 家
          </span>
        }
      />

      <form
        className="border-t-2 border-[var(--rule)] pt-6"
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <label
          className="mb-2 block text-[12.5px] font-medium text-[var(--text)]"
          htmlFor="shop-url"
        >
          公开店铺地址
        </label>
        <div className="flex gap-2">
          <input
            id="shop-url"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            disabled={mutationPending || shopsQuery.isLoading}
            placeholder="https://pay.ldxp.cn/shop/..."
            autoComplete="off"
            className={clsx(fieldClass, 'flex-1 font-mono placeholder:font-sans')}
          />
          <Button
            type="submit"
            variant="primary"
            size="lg"
            disabled={!url.trim() || duplicate || mutationPending || shopsQuery.isLoading}
          >
            {add.isPending ? (
              <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" />
            ) : (
              <Plus size={14} />
            )}
            {duplicate ? '已在店铺库' : add.isPending ? '正在抓取整店' : '添加并抓取'}
          </Button>
        </div>
      </form>

      {pendingText && (
        <div
          role="status"
          aria-live="polite"
          className="mt-3 flex items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--card)] px-4 py-2.5 text-[12.5px] text-[var(--text)]"
        >
          <LoaderCircle
            size={13}
            className="shrink-0 animate-spin text-[var(--ink)] motion-reduce:animate-none"
          />
          {pendingText}
        </div>
      )}

      <div className="mb-3 mt-6">
        <label className="relative block w-full max-w-[320px]">
          <Search
            size={14}
            className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-[var(--faint)]"
          />
          <input
            value={filter}
            onChange={(event) => {
              setFilter(event.target.value)
              setShopPage(1)
            }}
            placeholder="筛选店名或店铺 Token"
            aria-label="筛选店名或店铺 Token"
            className={clsx(fieldClass, 'pl-9')}
          />
        </label>
      </div>

      <div className="border-t-2 border-[var(--rule)]">
        <div
          className={clsx(
            'grid h-11 items-center gap-4 border-b border-[var(--border)] px-3 text-[13.5px] font-bold text-[var(--ink)]',
            GRID,
          )}
        >
          <div>店铺 / 地址</div>
          <div className="text-right">商品数</div>
          <div>最近同步</div>
          <div className="text-right">操作</div>
        </div>

        {shopsQuery.isLoading ? (
          <Spinner />
        ) : shopsQuery.isError ? (
          <EmptyState text={`无法读取店铺列表：${listError}`} />
        ) : visibleShops.length === 0 ? (
          <EmptyState
            text={shops.length === 0 ? '还没有零售店铺，在上方添加公开店铺链接' : '没有匹配的店铺'}
          />
        ) : (
          visibleShops.map((shop) => {
            const isRefreshing = refreshingId === shop.id
            const isDeleting = deletingId === shop.id
            return (
              <div
                key={shop.id}
                className={clsx(
                  'ui-row grid min-h-[66px] items-center gap-4 border-b border-[var(--border)] px-3 py-2 last:border-0',
                  GRID,
                )}
              >
                <div className="flex min-w-0 items-center gap-3">
                  <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-[var(--border-soft)] bg-[var(--surface)] text-[13px] font-semibold text-[var(--text)]">
                    {shopInitial(shop.name)}
                  </span>
                  <div className="min-w-0">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="truncate text-[15px] font-medium text-[var(--ink)]">
                        {shop.name}
                      </span>
                      {!shop.active && <StatusText label="已停用" tone="muted" />}
                    </div>
                    <div
                      className="truncate font-mono text-[12.5px] text-[var(--faint)]"
                      title={`${shop.url} · Token ${shop.token}`}
                    >
                      {shop.url}
                    </div>
                  </div>
                </div>

                <div className="num text-right text-[15.5px] font-semibold text-[var(--ink)]">
                  {shop.product_count.toLocaleString('zh-CN')}
                </div>

                <div className="text-[13.5px] text-[var(--muted)]" title={shop.last_synced_at ?? '尚未同步'}>
                  {relTime(shop.last_synced_at)}
                </div>

                <div className="flex justify-end gap-1">
                  <IconButton
                    type="button"
                    onClick={() => openShop(shop)}
                    aria-label={`打开店铺 ${shop.name}`}
                    title="打开店铺"
                  >
                    <ExternalLink size={14} />
                  </IconButton>
                  <IconButton
                    type="button"
                    disabled={syncPending || remove.isPending}
                    onClick={() => refreshOne.mutate(shop.id)}
                    aria-label={`刷新店铺 ${shop.name}`}
                    title="重新抓取整店"
                  >
                    {isRefreshing ? (
                      <LoaderCircle
                        size={14}
                        className="animate-spin text-[var(--accent)] motion-reduce:animate-none"
                      />
                    ) : (
                      <RefreshCw size={14} />
                    )}
                  </IconButton>
                  <IconButton
                    type="button"
                    tone="danger"
                    disabled={syncPending || remove.isPending}
                    aria-label={`删除店铺 ${shop.name}`}
                    title="删除"
                    onClick={() => {
                      if (window.confirm(`删除店铺「${shop.name}」及其已抓取商品？`)) {
                        remove.mutate(shop.id)
                      }
                    }}
                  >
                    {isDeleting ? (
                      <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" />
                    ) : (
                      <Trash2 size={14} />
                    )}
                  </IconButton>
                </div>
              </div>
            )
          })
        )}
      </div>

      {filteredShops.length > pageSize && (
        <div className="mt-3 flex items-center justify-between">
          <span className="tnum text-[12px] text-[var(--muted)]">
            第 {currentShopPage} / {shopPages} 页，匹配{' '}
            {filteredShops.length.toLocaleString('zh-CN')} 家
          </span>
          <div className="flex gap-1.5">
            <Button
              type="button"
              size="sm"
              disabled={currentShopPage <= 1}
              onClick={() => setShopPage((value) => Math.max(1, value - 1))}
            >
              上一页
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={currentShopPage >= shopPages}
              onClick={() => setShopPage((value) => Math.min(shopPages, value + 1))}
            >
              下一页
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
