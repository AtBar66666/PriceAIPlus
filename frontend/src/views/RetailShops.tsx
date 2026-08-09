import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { ExternalLink, LoaderCircle, Plus, RefreshCw, Search, Trash2 } from 'lucide-react'
import { api, type RetailShop } from '../lib/api'
import { relTime } from '../lib/format'
import { openExternal } from '../lib/openExternal'
import { toast } from '../lib/toast'
import { Button, EmptyState, PageHeader, Spinner, StatusTag } from '../components/ui'

const GRID = 'grid-cols-[minmax(180px,1fr)_90px_128px_204px]'

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
    <div>
      <PageHeader
        title="零售店铺"
        desc="这里保存原店 token；实时搜索命中的 PickAI 商品会自动解析并加入，后续直接抓原店，不再把聚合快照当库存。"
      />

      <form
        className="mb-6 flex flex-wrap items-end gap-3 rounded-[20px] border border-[var(--border-subtle)] bg-white p-5 shadow-[var(--shadow-card)]"
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <label className="min-w-[300px] flex-1">
          <span className="mb-2 block text-[12.5px] font-semibold text-[var(--text)]">公开店铺地址</span>
          <input
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            disabled={mutationPending || shopsQuery.isLoading}
            placeholder="https://pay.ldxp.cn/shop/..."
            autoComplete="off"
            className="ring-focus h-11 w-full rounded-full border border-[var(--border)] bg-white px-5 font-mono text-[13.5px] text-[var(--text)] shadow-[var(--shadow-xs)] outline-none transition-[border-color,box-shadow] duration-200 ease-out placeholder:font-sans placeholder:text-[var(--placeholder)] hover:border-[color-mix(in_srgb,var(--brand)_34%,var(--border))] hover:shadow-[var(--shadow-hover)] disabled:cursor-not-allowed disabled:bg-[var(--surface)] disabled:opacity-65"
          />
        </label>

        <Button
          type="submit"
          variant="dark"
          className="group"
          disabled={!url.trim() || duplicate || mutationPending || shopsQuery.isLoading}
        >
          {add.isPending ? (
            <LoaderCircle size={16} className="animate-spin motion-reduce:animate-none" />
          ) : (
            <Plus size={16} className="ui-icon-motion transition-transform duration-200 group-hover:scale-110" />
          )}
          {duplicate ? '已在店铺库' : add.isPending ? '正在抓取整店' : '添加并抓取'}
        </Button>

      </form>

      {pendingText && (
        <div
          role="status"
          aria-live="polite"
          className="mb-6 flex items-center gap-2.5 rounded-xl border border-[color-mix(in_srgb,var(--brand)_24%,transparent)] bg-[var(--brand-soft)] px-4 py-3 text-[13px] font-medium text-[var(--success-text)]"
        >
          <LoaderCircle size={16} className="shrink-0 animate-spin motion-reduce:animate-none" />
          {pendingText}
        </div>
      )}

      <div className="mb-3 flex items-center justify-between gap-4">
        <label className="relative block w-full max-w-[420px]">
          <Search
            size={15}
            className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-[var(--placeholder)]"
          />
          <input
            value={filter}
            onChange={(event) => {
              setFilter(event.target.value)
              setShopPage(1)
            }}
            placeholder="筛选店名或店铺 Token"
            className="ring-focus h-10 w-full rounded-full border border-[var(--border)] bg-white pl-10 pr-4 text-[13px] text-[var(--text)] outline-none placeholder:text-[var(--placeholder)]"
          />
        </label>
        <span className="shrink-0 text-[12.5px] text-[var(--soft)]">
          已收录 {shops.length.toLocaleString('zh-CN')} 家
        </span>
      </div>

      <div className="overflow-hidden rounded-[20px] border border-[var(--border-subtle)] bg-white shadow-[var(--shadow-card)]">
        <div
          className={clsx(
            'grid h-[52px] items-center gap-5 border-b border-[var(--border-subtle)] bg-[var(--panel-soft)] px-6',
            GRID,
          )}
        >
          <div className="t-label">店铺 / 地址</div>
          <div className="t-label">商品</div>
          <div className="t-label">最近同步</div>
          <div className="t-label text-right">操作</div>
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
                  'ui-row-soft grid min-h-[84px] items-center gap-5 border-b border-[var(--border-subtle)] px-6 py-3 last:border-0',
                  GRID,
                )}
              >
                <div className="min-w-0">
                  <div className="flex min-w-0 items-center gap-2">
                    <div className="truncate text-[15px] font-semibold text-[var(--ink)]">{shop.name}</div>
                    <StatusTag label={shop.active ? '已启用' : '已停用'} tone={shop.active ? 'ok' : 'muted'} />
                  </div>
                  <div
                    className="mt-1 truncate font-mono text-[11.5px] text-[var(--soft)]"
                    title={`${shop.url} · Token ${shop.token}`}
                  >
                    {shop.url} · Token {shop.token}
                  </div>
                </div>

                <div>
                  <div className="num text-[17px] font-semibold text-[var(--ink)]">
                    {shop.product_count.toLocaleString('zh-CN')}
                  </div>
                  <div className="mt-0.5 text-[11.5px] text-[var(--placeholder)]">条商品</div>
                </div>

                <div
                  className="text-[12.5px] font-medium text-[var(--muted)]"
                  title={shop.last_synced_at ?? '尚未同步'}
                >
                  {relTime(shop.last_synced_at)}
                </div>

                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="group w-[72px] px-0"
                    onClick={() => openShop(shop)}
                  >
                    <ExternalLink
                      size={14}
                      className="ui-icon-motion transition-transform duration-200 group-hover:translate-x-0.5 group-hover:-translate-y-0.5"
                    />
                    打开
                  </Button>

                  <Button
                    type="button"
                    size="sm"
                    className="group w-[72px] px-0"
                    disabled={syncPending || remove.isPending}
                    onClick={() => refreshOne.mutate(shop.id)}
                  >
                    {isRefreshing ? (
                      <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" />
                    ) : (
                      <RefreshCw
                        size={14}
                        className="ui-icon-motion transition-transform duration-200 group-hover:rotate-[18deg]"
                      />
                    )}
                    {isRefreshing ? '抓取中' : '刷新'}
                  </Button>

                  <Button
                    type="button"
                    size="sm"
                    variant="danger"
                    className="w-9 px-0"
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
                  </Button>
                </div>
              </div>
            )
          })
        )}
      </div>
      {filteredShops.length > pageSize && (
        <div className="mt-4 flex items-center justify-between">
          <span className="text-[12.5px] text-[var(--soft)]">
            第 {currentShopPage}/{shopPages} 页 · 匹配 {filteredShops.length.toLocaleString('zh-CN')} 家
          </span>
          <div className="flex gap-2">
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={currentShopPage <= 1}
              onClick={() => setShopPage((value) => Math.max(1, value - 1))}
            >
              上一页
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
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
