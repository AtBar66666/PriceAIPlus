import {
  lazy,
  memo,
  Suspense,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import clsx from 'clsx'
import { animate, motion, useReducedMotion } from 'motion/react'
import {
  AlertTriangle,
  ArrowRight,
  ArrowUpRight,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react'
import { api, type Product } from '../lib/api'
import { Button, Dot, IconButton, Switch } from '../components/ui'
import { cny, relTime } from '../lib/format'
import { SHORTCUTS, shortcutId } from '../lib/shortcuts'

type Platform = 'all' | 'ldxp' | 'catfk'

const IS_TAURI = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
const ProductDrawer = lazy(() =>
  import('./ProductDrawer').then((module) => ({ default: module.ProductDrawer })),
)

function isStrictRealtimeSearch(value: string): boolean {
  const compact = value.toLowerCase().replace(/\s+/g, '')
  return /k12|邮箱|email|mail|gmail|outlook|hotmail|icloud|接码|sms|chatgpt|gpt|plus|team|business|pro|codex|普号|代充|充值|周边/.test(
    compact,
  )
}

const ProductRow = memo(function ProductRow({
  product,
  rank,
  isLowest,
  reduceMotion,
  onOpen,
}: {
  product: Product
  rank: number
  isLowest: boolean
  reduceMotion: boolean
  onOpen: (product: Product) => void
}) {
  return (
    <motion.div
      role="button"
      tabIndex={0}
      onClick={() => onOpen(product)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') onOpen(product)
      }}
      initial={reduceMotion ? false : { opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        duration: 0.5,
        delay: Math.min(rank - 1, 12) * 0.045,
        ease: [0.22, 1, 0.36, 1],
      }}
      className="prow flex cursor-pointer items-center gap-5 px-3 py-[18px] outline-none"
    >
      <span className="num r-faint w-9 shrink-0 text-[13.5px] font-semibold">
        {String(rank).padStart(2, '0')}
      </span>
      <div className="min-w-0 flex-1">
        <div className="r-ink truncate text-[16.5px] font-semibold leading-[1.45] tracking-[-0.012em]">
          {product.name}
        </div>
        <div className="r-muted mt-1 flex min-w-0 items-center gap-1.5 text-[13.5px] leading-[1.4]">
          <span className="truncate">{product.merchant_name || '未命名店铺'}</span>
          <span className="r-faint shrink-0" title={product.verified_at || undefined}>
            · {product.verified_at ? `核验于 ${relTime(product.verified_at)}` : '本轮核验'}
          </span>
        </div>
      </div>

      {isLowest && (
        <span className="r-blue shrink-0 text-[13px] font-bold" title="本轮最低价">
          最低
        </span>
      )}
      <span className="num-lg r-price w-[128px] shrink-0 text-right">
        {cny(product.sale_price)}
      </span>

      <div className="flex w-[110px] shrink-0 items-center justify-end">
        <RowStock product={product} />
      </div>

      <ArrowUpRight size={18} strokeWidth={2} className="r-faint shrink-0" />
    </motion.div>
  )
})

function RowStock({ product }: { product: Product }) {
  if (product.status === '未上架')
    return <span className="r-faint text-[13.5px] font-medium">未上架</span>
  if (product.status === '缺货' || product.stock === 0)
    return <span className="r-bad text-[13.5px] font-bold">缺货</span>
  if (product.stock < 0)
    return <span className="r-faint text-[13.5px] font-medium">库存未知</span>
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="r-muted text-[12.5px]">有货</span>
      <span className="num r-ink text-[15px] font-bold">
        {product.stock.toLocaleString('zh-CN')}
      </span>
    </span>
  )
}

function LoadingRows() {
  return (
    <div aria-label="正在核验原店报价" aria-live="polite">
      {Array.from({ length: 6 }, (_, index) => (
        <div
          key={index}
          className="flex animate-pulse items-center gap-5 border-b border-[var(--border)] px-3 py-[22px] motion-reduce:animate-none"
        >
          <i className="block h-3.5 w-9 shrink-0 rounded bg-[var(--surface)]" />
          <div className="min-w-0 flex-1">
            <i className="block h-4 w-[52%] rounded bg-[var(--surface)]" />
            <i className="mt-2.5 block h-3 w-[28%] rounded bg-[var(--surface)]" />
          </div>
          <i className="block h-5 w-24 shrink-0 rounded bg-[var(--surface)]" />
          <i className="block h-4 w-16 shrink-0 rounded bg-[var(--surface)]" />
        </div>
      ))}
    </div>
  )
}

function InitialState({
  onSearch,
  reduceMotion,
}: {
  onSearch: (query: string) => void
  reduceMotion: boolean
}) {
  return (
    <div className="pt-12">
      <div className="text-[14px] font-medium text-[var(--faint)]">快捷分类</div>
      <div className="mt-5 flex flex-wrap gap-3.5">
        {SHORTCUTS.map((item, index) => (
          <motion.button
            key={item.id}
            type="button"
            onClick={() => onSearch(item.query)}
            initial={reduceMotion ? false : { opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.1 + index * 0.06, ease: [0.22, 1, 0.36, 1] }}
            className="cat-btn rounded-full px-7 py-4 text-left"
          >
            <span className="text-[18px] font-bold tracking-[-0.01em]">{item.label}</span>
            <span className="ml-3 text-[13.5px] font-normal opacity-55">{item.detail}</span>
          </motion.button>
        ))}
      </div>
      <p className="mt-12 max-w-[520px] text-[14.5px] leading-[1.7] text-[var(--muted)]">
        比牌只做一件事：同时敲开几十家原店的门，把它们此刻真实返回的售价与库存排成一张榜单。
        不缓存、不凑数，每一行都来自本轮响应。
      </p>
    </div>
  )
}

function NoResults({ challenged }: { challenged: boolean }) {
  return (
    <div className="px-3 py-16">
      <div className="text-[20px] font-bold tracking-[-0.015em] text-[var(--ink)]">
        {challenged ? '源站拦截了核验，不代表没货' : '没有找到已核验的有货商品'}
      </div>
      <p className="mt-2.5 max-w-[520px] text-[14.5px] leading-[1.7] text-[var(--muted)]">
        {challenged
          ? '链动小铺当前返回阿里云滑块。点击上方「拖一次，自动重搜」，完成一次真人验证后程序会接管。'
          : '这里只显示原店本轮明确返回的结果，不会拿缓存商品凑数。'}
      </p>
    </div>
  )
}

function CountUp({ value, formatter }: { value: number; formatter: (n: number) => string }) {
  const reduce = useReducedMotion()
  const [display, setDisplay] = useState(() => (reduce ? value : 0))
  useEffect(() => {
    if (reduce) {
      setDisplay(value)
      return
    }
    const controls = animate(0, value, {
      duration: 0.85,
      ease: [0.22, 1, 0.36, 1],
      onUpdate: (latest) => setDisplay(latest),
    })
    return () => controls.stop()
  }, [value, reduce])
  return <>{formatter(display)}</>
}

function StatCol({
  label,
  first = false,
  children,
}: {
  label: string
  first?: boolean
  children: ReactNode
}) {
  return (
    <div className={clsx('min-w-0 flex-1', !first && 'border-l border-[var(--border-2)] pl-9')}>
      <div className="text-[13.5px] font-medium text-[var(--faint)]">{label}</div>
      <div className="num-hero mt-2 flex items-baseline gap-2.5 whitespace-nowrap">{children}</div>
    </div>
  )
}

export function Products({
  keyword,
  searchSeq,
  onSearch,
  onOpenSettings,
}: {
  keyword: string
  searchSeq: number
  onSearch: (query: string) => void
  onOpenSettings?: () => void
}) {
  const qc = useQueryClient()
  const reduce = useReducedMotion()
  const [search, setSearch] = useState(keyword)
  const [sort, setSort] = useState<'sale_asc' | 'stock_desc'>('sale_asc')
  const [inStock, setInStock] = useState(true)
  const [platform, setPlatform] = useState<Platform>('all')
  const [page, setPage] = useState(1)
  const [refreshSeq, setRefreshSeq] = useState(0)
  const [active, setActive] = useState<Product | null>(null)
  const [verificationPending, setVerificationPending] = useState(false)
  const [verificationMessage, setVerificationMessage] = useState('')
  const searchInput = useRef<HTMLInputElement>(null)
  const pageSize = 20

  const isLive = keyword.trim().length > 0
  const strictRealtimeSearch = isStrictRealtimeSearch(keyword)
  const activeShortcutInfo = SHORTCUTS.find((item) => item.id === shortcutId(keyword))

  useEffect(() => {
    setSearch(keyword)
    setPage(1)
  }, [keyword, searchSeq])

  const searchParams = useMemo(
    () => ({
      keywords: keyword,
      in_stock: inStock,
      page,
      page_size: pageSize,
      sort,
      platform,
    }),
    [inStock, keyword, page, platform, sort],
  )

  const liveQuery = useQuery({
    queryKey: [
      'strict-realtime-search',
      searchSeq,
      refreshSeq,
      keyword,
      inStock,
      sort,
      page,
      platform,
    ],
    queryFn: ({ signal }) => api.liveSearch(searchParams, signal),
    enabled: isLive,
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

  const resultData = liveQuery.data

  useEffect(() => {
    if (!resultData) return
    if (typeof resultData.retail_index?.indexed_shops === 'number')
      qc.setQueryData(['retail-index'], resultData.retail_index)
    if (typeof resultData.pickai_index?.product_count === 'number')
      qc.setQueryData(['pickai-index'], resultData.pickai_index)
  }, [qc, resultData])

  const openPublicVerification = async () => {
    if (!IS_TAURI) {
      setVerificationMessage('请在比牌桌面版中完成真人验证。')
      return
    }
    setVerificationPending(true)
    setVerificationMessage('正在打开原站滑块…')
    try {
      await invoke('open_public_verification')
    } catch (error) {
      setVerificationPending(false)
      setVerificationMessage(error instanceof Error ? error.message : String(error))
    }
  }

  useEffect(() => {
    if (!IS_TAURI) return
    let disposed = false
    let unlisteners: UnlistenFn[] = []
    void Promise.all([
      listen<{ message: string }>('public-verification-progress', (event) => {
        setVerificationPending(true)
        setVerificationMessage(event.payload.message)
      }),
      listen<{ message: string }>('public-verification-complete', (event) => {
        setVerificationPending(false)
        setVerificationMessage(event.payload.message)
        setPage(1)
        setRefreshSeq((current) => current + 1)
      }),
      listen<{ message: string }>('public-verification-error', (event) => {
        setVerificationPending(false)
        setVerificationMessage(event.payload.message)
      }),
    ]).then((registered) => {
      if (disposed) registered.forEach((unlisten) => unlisten())
      else unlisteners = registered
    })
    return () => {
      disposed = true
      unlisteners.forEach((unlisten) => unlisten())
    }
  }, [])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        searchInput.current?.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const items = (resultData?.items ?? []).filter((product) => product.verified)
  const total = resultData?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const lowestPrice = items.reduce<number | null>(
    (lowest, product) =>
      product.sale_price > 0 && (lowest === null || product.sale_price < lowest)
        ? product.sale_price
        : lowest,
    null,
  )
  const inStockCount = items.filter(
    (product) =>
      product.stock !== 0 && product.status !== '缺货' && product.status !== '未上架',
  ).length
  const searching = isLive && liveQuery.isFetching
  const challenged = Boolean(
    resultData?.warnings?.some((warning) =>
      /滑块|验证页|网页验证|访问保护冷却|站点保护冷却|库存(?:暂)?无法确认|库存暂不可确认|未能完成.*核验|未返回可核验商品/.test(
        warning,
      ),
    ),
  )
  const title = activeShortcutInfo?.label || (keyword ? `“${keyword}”` : '实时商品搜索')

  useEffect(() => {
    if (resultData && page > pages) setPage(pages)
  }, [page, pages, resultData])

  const commit = () => onSearch(search)

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 px-10 pt-8">
        <div className="mx-auto w-full max-w-[980px]">
        <div className="flex items-end justify-between gap-6">
          <div className="min-w-0">
            <div className="flex min-w-0 items-baseline gap-4">
              <motion.h1
                key={title}
                initial={reduce ? false : { y: 28, opacity: 0 }}
                animate={{ y: 0, opacity: 1 }}
                transition={{ duration: 0.55, ease: [0.22, 1, 0.36, 1] }}
                className="t-display truncate"
              >
                {title}
              </motion.h1>
              {isLive && strictRealtimeSearch && (
                <span className="inline-flex shrink-0 items-center gap-1.5 text-[13.5px] font-bold text-[var(--ok)]">
                  <ShieldCheck size={15} />
                  严格实时
                </span>
              )}
            </div>
            <div className="mt-3 flex items-center gap-2.5 text-[14.5px] text-[var(--muted)]">
              {!isLive ? (
                <span>选一个快捷分类，或在下方输入关键词</span>
              ) : searching ? (
                <span className="inline-flex items-center gap-1.5 text-[var(--accent)]">
                  <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" />
                  正在并行核验多家低价原店，首轮通常 4-8 秒
                </span>
              ) : liveQuery.isError ? (
                <span className="text-[var(--bad)]">本次实时搜索失败</span>
              ) : challenged ? (
                <span className="inline-flex items-center gap-1.5 text-[var(--warn)]">
                  <AlertTriangle size={14} />
                  原店未完成核验，旧数据已隐藏
                </span>
              ) : (
                <span className="inline-flex items-center gap-2 text-[var(--muted)]">
                  <Dot tone="ok" />
                  本轮核验完成，价格与库存来自原店实时响应
                </span>
              )}
            </div>
          </div>

          {isLive && (
            <div className="flex shrink-0 items-center gap-7 pb-1.5">
              <div className="relative">
                <select
                  value={platform}
                  onChange={(event) => {
                    setPlatform(event.target.value as Platform)
                    setPage(1)
                  }}
                  aria-label="搜索来源"
                  className="ui-control cursor-pointer appearance-none bg-transparent pr-5 text-[14px] font-medium text-[var(--muted)] outline-none hover:text-[var(--ink)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--blue)]"
                >
                  <option value="all">全部来源</option>
                  <option value="ldxp">链动小铺</option>
                  <option value="catfk">云猫寄售</option>
                </select>
                <ChevronDown
                  size={14}
                  className="pointer-events-none absolute right-0 top-1/2 -translate-y-1/2 text-[var(--faint)]"
                />
              </div>
              <div className="flex items-center gap-5">
                {(
                  [
                    { key: 'sale_asc', label: '最低价' },
                    { key: 'stock_desc', label: '库存' },
                  ] as const
                ).map((option) => (
                  <button
                    key={option.key}
                    type="button"
                    onClick={() => {
                      setSort(option.key)
                      setPage(1)
                    }}
                    className={clsx(
                      'ui-control relative pb-1 text-[14px] leading-none',
                      sort === option.key
                        ? 'font-bold text-[var(--ink)] after:absolute after:bottom-0 after:left-0 after:h-[2px] after:w-full after:bg-[var(--ink)]'
                        : 'font-medium text-[var(--muted)] hover:text-[var(--ink)]',
                    )}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              <label className="flex cursor-pointer select-none items-center gap-2.5 text-[14px] font-medium text-[var(--text)]">
                <Switch
                  checked={inStock}
                  onChange={() => {
                    setInStock((current) => !current)
                    setPage(1)
                  }}
                />
                仅看有货
              </label>
              {!searching && (
                <IconButton
                  onClick={() => setRefreshSeq((current) => current + 1)}
                  aria-label="重新实时核验"
                  title="重新实时核验"
                >
                  <RefreshCw size={16} />
                </IconButton>
              )}
            </div>
          )}
        </div>

        <div className="search-line mt-7 flex items-center gap-5 pb-3.5">
          <input
            ref={searchInput}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            onKeyDown={(event) => event.key === 'Enter' && commit()}
            placeholder="输入 K12、ChatGPT Plus、邮箱或 OpenAI 接码"
            className="min-w-0 flex-1 bg-transparent text-[clamp(17px,1.6vw,21px)] font-medium tracking-[-0.01em] text-[var(--ink)] outline-none placeholder:text-[var(--placeholder)]"
          />
          <kbd className="num hidden shrink-0 text-[12px] font-semibold tracking-[0.06em] text-[var(--faint)] lg:block">
            CTRL K
          </kbd>
          <button
            type="button"
            onClick={commit}
            disabled={!search.trim() || searching}
            aria-label="搜索"
            className="ui-control grid h-11 w-11 shrink-0 place-items-center bg-[var(--ink)] text-[var(--bone)] hover:bg-[var(--blue)] disabled:pointer-events-none disabled:opacity-30"
          >
            {searching ? (
              <LoaderCircle size={18} className="animate-spin motion-reduce:animate-none" />
            ) : (
              <ArrowRight size={19} strokeWidth={2.2} />
            )}
          </button>
        </div>

        {isLive && !liveQuery.isError && (
          <div className="flex gap-9 pb-7 pt-7">
            {searching ? (
              Array.from({ length: 4 }, (_, index) => (
                <div
                  key={index}
                  className={clsx(
                    'flex-1 animate-pulse motion-reduce:animate-none',
                    index > 0 && 'border-l border-[var(--border-2)] pl-9',
                  )}
                >
                  <i className="block h-3.5 w-20 rounded bg-[var(--surface)]" />
                  <i className="mt-3 block h-9 w-28 rounded bg-[var(--surface)]" />
                </div>
              ))
            ) : (
              <>
                <StatCol label="已核验商品" first>
                  <CountUp value={total} formatter={(n) => Math.round(n).toLocaleString('zh-CN')} />
                </StatCol>
                <StatCol label="本轮最低价">
                  {lowestPrice !== null ? (
                    <>
                      <span className="text-[var(--blue)]">
                        <CountUp value={lowestPrice} formatter={cny} />
                      </span>
                      <span className="text-[14px] font-bold tracking-normal text-[var(--blue)]">
                        最低
                      </span>
                    </>
                  ) : (
                    '-'
                  )}
                </StatCol>
                <StatCol label="本页有货">
                  <CountUp
                    value={inStockCount}
                    formatter={(n) => Math.round(n).toLocaleString('zh-CN')}
                  />
                  <span className="text-[18px] font-bold text-[var(--faint)]">/ {items.length}</span>
                </StatCol>
                <StatCol label="核验时间">
                  <span className="tracking-[-0.02em]">
                    {resultData?.refreshed_at ? relTime(resultData.refreshed_at) : '刚刚'}
                  </span>
                </StatCol>
              </>
            )}
          </div>
        )}

        {isLive && !searching && Boolean(resultData?.warnings?.length) && (
          <div className="mt-4 flex items-start gap-2.5 rounded-lg border border-[var(--warn-border)] bg-[var(--warn-bg)] px-3.5 py-3 text-[13.5px] leading-[1.55] text-[var(--warn)]">
            <AlertTriangle size={15} className="mt-0.5 shrink-0" />
            <span className="min-w-0 flex-1">
              {resultData?.warnings?.join('；')}
              {challenged && ' 已隐藏所有未核验的旧价格和延迟库存。'}
              {challenged && verificationMessage && (
                <span className="ml-1 font-medium">{verificationMessage}</span>
              )}
            </span>
            {challenged && (
              <button
                type="button"
                onClick={() => void openPublicVerification()}
                disabled={verificationPending}
                className="ui-control inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border border-[var(--warn-border)] bg-[var(--card)] px-3 text-[13px] font-medium text-[var(--warn)] hover:border-[var(--warn)] disabled:cursor-wait disabled:opacity-60"
              >
                {verificationPending ? (
                  <LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" />
                ) : (
                  <ShieldCheck size={13} />
                )}
                {verificationPending ? '等你拖滑块' : '拖一次，自动重搜'}
              </button>
            )}
            {onOpenSettings &&
              resultData?.warnings?.some((warning) =>
                /Merchant-Token|未登录|登录已失效|重新登录/i.test(warning),
              ) && (
                <button
                  type="button"
                  onClick={onOpenSettings}
                  className="shrink-0 font-medium underline underline-offset-2"
                >
                  连接设置
                </button>
              )}
          </div>
        )}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-10 pb-4">
        <div className="mx-auto w-full max-w-[980px]">
          {!isLive ? (
            <InitialState onSearch={onSearch} reduceMotion={Boolean(reduce)} />
          ) : (
            <div className="border-t-2 border-[var(--rule)]">
              {searching ? (
                <LoadingRows />
              ) : liveQuery.isError ? (
                <div className="px-3 py-16">
                  <div className="text-[20px] font-bold tracking-[-0.015em] text-[var(--ink)]">
                    实时搜索失败
                  </div>
                  <p className="mt-2.5 text-[14.5px] text-[var(--muted)]">
                    没有展示缓存结果。点击右上角刷新后重新核验。
                  </p>
                </div>
              ) : items.length === 0 ? (
                <NoResults challenged={challenged} />
              ) : (
                items.map((product, index) => (
                  <ProductRow
                    key={product.id}
                    product={product}
                    rank={index + 1}
                    reduceMotion={Boolean(reduce)}
                    isLowest={lowestPrice !== null && product.sale_price === lowestPrice}
                    onOpen={setActive}
                  />
                ))
              )}
            </div>
          )}
        </div>
      </div>

      {isLive && !searching && total > 0 && (
        <footer className="shrink-0 border-t border-[var(--border)] px-10">
          <div className="mx-auto flex h-14 w-full max-w-[980px] items-center justify-between text-[13.5px] text-[var(--muted)]">
            <span>
              第 <span className="num font-semibold text-[var(--ink)]">{page}</span> /{' '}
              <span className="num font-semibold text-[var(--ink)]">{pages}</span> 页，本页{' '}
              <span className="num font-semibold text-[var(--ink)]">{items.length}</span> 条已核验
            </span>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                size="sm"
                disabled={page <= 1}
                onClick={() => setPage(page - 1)}
              >
                <ChevronLeft size={14} />
                上一页
              </Button>
              <Button
                type="button"
                size="sm"
                disabled={page >= pages}
                onClick={() => setPage(page + 1)}
              >
                下一页
                <ChevronRight size={14} />
              </Button>
            </div>
          </div>
        </footer>
      )}

      {active && (
        <Suspense fallback={null}>
          <ProductDrawer product={active} onClose={() => setActive(null)} />
        </Suspense>
      )}
    </div>
  )
}
