import {
  lazy,
  memo,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { useQuery } from '@tanstack/react-query'
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import clsx from 'clsx'
import {
  AlertTriangle,
  ArrowUpRight,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CirclePlus,
  Clock3,
  GraduationCap,
  LoaderCircle,
  Mail,
  MessageSquareText,
  PackageSearch,
  RefreshCw,
  Search,
  ShieldCheck,
} from 'lucide-react'
import {
  api,
  type PickAIIndexStatus,
  type Product,
  type RetailIndexStatus,
} from '../lib/api'
import { BrandIcon } from '../components/BrandIcon'
import { StockPill } from '../components/ui'
import { cny, relTime } from '../lib/format'

type Platform = 'all' | 'ldxp' | 'catfk'

const SHORTCUTS = [
  {
    id: 'k12',
    label: 'K12',
    query: 'K12',
    detail: 'GPT Team 与教育版',
    icon: GraduationCap,
  },
  {
    id: 'plus',
    label: 'ChatGPT Plus',
    query: 'ChatGPT Plus',
    detail: '独享号、订阅与成品号',
    icon: CirclePlus,
  },
  {
    id: 'email',
    label: '邮箱',
    query: '邮箱',
    detail: 'Gmail、Outlook 等',
    icon: Mail,
  },
  {
    id: 'sms',
    label: 'OpenAI 接码',
    query: 'OpenAI 接码',
    detail: '只查 ChatGPT 验证码',
    icon: MessageSquareText,
  },
] as const

const GRID = 'grid-cols-[minmax(260px,1fr)_108px_128px_76px]'
const IS_TAURI = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
const ProductDrawer = lazy(() =>
  import('./ProductDrawer').then((module) => ({ default: module.ProductDrawer })),
)

function shortcutId(value: string): string | null {
  const compact = value.toLowerCase().replace(/\s+/g, '')
  if (compact.includes('k12')) return 'k12'
  if (/邮箱|email|gmail|outlook|hotmail|icloud/.test(compact)) return 'email'
  if (compact.includes('接码') || compact.includes('sms')) return 'sms'
  if (compact.includes('plus')) return 'plus'
  return null
}

function isStrictRealtimeSearch(value: string): boolean {
  const compact = value.toLowerCase().replace(/\s+/g, '')
  return /k12|邮箱|email|mail|gmail|outlook|hotmail|icloud|接码|sms|chatgpt|gpt|plus|team|business|pro|codex|普号|代充|充值|周边/.test(
    compact,
  )
}

function isRetailIndexRunning(status?: RetailIndexStatus): boolean {
  return Boolean(
    status?.running || status?.state === 'discovering' || status?.state === 'indexing',
  )
}

const ProductRow = memo(function ProductRow({
  product,
  onOpen,
}: {
  product: Product
  onOpen: (product: Product) => void
}) {
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onOpen(product)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') onOpen(product)
      }}
      className={clsx(
        'result-row grid min-h-[76px] cursor-pointer items-center gap-4 border-b border-[var(--border-subtle)] px-5 outline-none last:border-b-0',
        GRID,
      )}
    >
      <div className="flex min-w-0 items-center gap-3.5 py-3">
        <BrandIcon name={product.name} category={product.category} />
        <div className="min-w-0">
          <div className="truncate text-[14px] font-semibold leading-5 text-[var(--ink)]">
            {product.name}
          </div>
          <div className="mt-1 flex min-w-0 items-center gap-2 text-[12px] text-[var(--soft)]">
            <span className="truncate">{product.merchant_name || '未命名店铺'}</span>
            <span className="shrink-0 text-[var(--border-strong)]">/</span>
            <span
              className="inline-flex shrink-0 items-center gap-1 text-[var(--success-text)]"
              title={product.verified_at || undefined}
            >
              <CheckCircle2 size={12} strokeWidth={2.2} />
              {product.verified_at ? relTime(product.verified_at) : '本轮核验'}
            </span>
          </div>
        </div>
      </div>

      <div className="num text-right text-[18px] font-bold tracking-[-0.02em] text-[var(--ink)]">
        {cny(product.sale_price)}
      </div>

      <div>
        <StockPill stock={product.stock} status={product.status} />
      </div>

      <div className="flex justify-end">
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            onOpen(product)
          }}
          className="ui-control inline-flex h-8 items-center gap-1 rounded-lg border border-[var(--border)] bg-white px-2.5 text-[12px] font-semibold text-[var(--text)] hover:border-[var(--brand)] hover:text-[var(--brand)]"
        >
          详情
          <ArrowUpRight size={13} />
        </button>
      </div>
    </div>
  )
})

function LoadingRows() {
  return (
    <div aria-label="正在核验原店报价" aria-live="polite">
      {Array.from({ length: 6 }, (_, index) => (
        <div
          key={index}
          className={clsx(
            'grid min-h-[76px] animate-pulse items-center gap-4 border-b border-[var(--border-subtle)] px-5 last:border-b-0 motion-reduce:animate-none',
            GRID,
          )}
        >
          <div className="flex items-center gap-3.5">
            <i className="h-10 w-10 rounded-lg bg-[var(--surface)]" />
            <div className="flex-1">
              <i className="block h-3.5 w-[58%] rounded bg-[var(--surface)]" />
              <i className="mt-2 block h-2.5 w-[34%] rounded bg-[var(--surface)]" />
            </div>
          </div>
          <i className="ml-auto block h-4 w-16 rounded bg-[var(--surface)]" />
          <i className="block h-7 w-20 rounded bg-[var(--surface)]" />
          <i className="ml-auto block h-8 w-14 rounded-lg bg-[var(--surface)]" />
        </div>
      ))}
    </div>
  )
}

function InitialState({ onSearch }: { onSearch: (query: string) => void }) {
  return (
    <div className="flex min-h-[360px] flex-1 items-center justify-center px-8 py-12">
      <div className="max-w-[510px] text-center">
        <div className="mx-auto grid h-12 w-12 place-items-center rounded-xl border border-[var(--border)] bg-white text-[var(--brand)]">
          <PackageSearch size={22} strokeWidth={1.9} />
        </div>
        <h2 className="mt-5 text-[19px] font-semibold tracking-[-0.015em] text-[var(--ink)]">
          选择左侧分类，直接查原店
        </h2>
        <p className="mx-auto mt-2 max-w-[440px] text-[13px] leading-6 text-[var(--muted)]">
          候选目录只负责找到店铺。表里的售价、库存和上架状态，都来自这次搜索触发的原店响应。
        </p>
        <div className="mt-6 flex flex-wrap justify-center gap-2">
          {SHORTCUTS.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => onSearch(item.query)}
              className="ui-control h-9 rounded-lg border border-[var(--border)] bg-white px-3.5 text-[12.5px] font-semibold text-[var(--text)] hover:border-[var(--brand)] hover:text-[var(--brand)]"
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

function NoResults({ challenged }: { challenged: boolean }) {
  return (
    <div className="flex min-h-[330px] items-center justify-center px-8 text-center">
      <div className="max-w-[440px]">
        <div className="mx-auto grid h-11 w-11 place-items-center rounded-xl bg-[var(--surface)] text-[var(--soft)]">
          {challenged ? <AlertTriangle size={20} /> : <PackageSearch size={20} />}
        </div>
        <h3 className="mt-4 text-[15px] font-semibold text-[var(--ink)]">
          {challenged ? '源站拦截了核验，不代表没货' : '没有找到已核验的有货商品'}
        </h3>
        <p className="mt-1.5 text-[12.5px] leading-5 text-[var(--muted)]">
          {challenged
            ? '链动小铺当前返回阿里云滑块。点击上方“拖一次，自动重搜”，完成一次真人验证后程序会接管。'
            : '这里只显示原店本轮明确返回的结果，不会拿缓存商品凑数。'}
        </p>
      </div>
    </div>
  )
}

function SourceSummary({
  retail,
  pickai,
}: {
  retail?: RetailIndexStatus
  pickai?: PickAIIndexStatus
}) {
  const retailRunning = isRetailIndexRunning(retail)
  return (
    <div className="border-t border-[var(--border-subtle)] px-4 py-4">
      <div className="mb-2 text-[10.5px] font-bold uppercase tracking-[0.11em] text-[var(--soft)]">
        候选目录
      </div>
      <div className="space-y-2 text-[11.5px] text-[var(--muted)]">
        <div className="flex items-center justify-between gap-3">
          <span className="inline-flex items-center gap-1.5">
            {retailRunning ? (
              <LoaderCircle size={12} className="animate-spin motion-reduce:animate-none" />
            ) : (
              <i className="h-1.5 w-1.5 rounded-full bg-[var(--success-text)]" />
            )}
            零售店
          </span>
          <span className="num text-[var(--text)]">
            {typeof retail?.indexed_shops === 'number'
              ? retail.indexed_shops.toLocaleString('zh-CN')
              : '读取中'}
          </span>
        </div>
        <div className="flex items-center justify-between gap-3">
          <span className="inline-flex items-center gap-1.5">
            <i
              className={clsx(
                'h-1.5 w-1.5 rounded-full',
                pickai?.state === 'error' ? 'bg-[var(--danger-text)]' : 'bg-[var(--brand)]',
              )}
            />
            PickAI 分类
          </span>
          <span className="num text-[var(--text)]">
            {typeof pickai?.product_count === 'number'
              ? pickai.product_count.toLocaleString('zh-CN')
              : '读取中'}
          </span>
        </div>
      </div>
    </div>
  )
}

export function Products({ onOpenSettings }: { onOpenSettings?: () => void }) {
  const [search, setSearch] = useState('')
  const [keyword, setKeyword] = useState('')
  const [sort, setSort] = useState<'sale_asc' | 'stock_desc'>('sale_asc')
  const [inStock, setInStock] = useState(true)
  const [platform, setPlatform] = useState<Platform>('all')
  const [page, setPage] = useState(1)
  const [revision, setRevision] = useState(0)
  const [active, setActive] = useState<Product | null>(null)
  const [verificationPending, setVerificationPending] = useState(false)
  const [verificationMessage, setVerificationMessage] = useState('')
  const searchInput = useRef<HTMLInputElement>(null)
  const pageSize = 20

  const isLive = keyword.trim().length > 0
  const strictRealtimeSearch = isStrictRealtimeSearch(keyword)
  const activeShortcut = shortcutId(keyword)
  const activeShortcutInfo = SHORTCUTS.find((item) => item.id === activeShortcut)

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
    queryKey: ['strict-realtime-search', revision, keyword, inStock, sort, page, platform],
    queryFn: ({ signal }) => api.liveSearch(searchParams, signal),
    enabled: isLive,
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

  const retailIndexQuery = useQuery({
    queryKey: ['retail-index'],
    queryFn: api.retailIndex,
    retry: false,
    refetchInterval: (query) => (isRetailIndexRunning(query.state.data) ? 3_000 : false),
  })
  const pickaiIndexQuery = useQuery({
    queryKey: ['pickai-index'],
    queryFn: api.pickaiIndex,
    retry: false,
    refetchInterval: (query) => (query.state.data?.running ? 2_000 : false),
  })

  const startSearch = useCallback((value: string) => {
    const next = value.trim()
    if (!next) return
    setSearch(next)
    setKeyword(next)
    setPage(1)
    setRevision((current) => current + 1)
  }, [])

  const commit = () => startSearch(search)
  const changePage = (next: number) => {
    setPage(next)
    setRevision((current) => current + 1)
  }
  const changeSort = (next: 'sale_asc' | 'stock_desc') => {
    setSort(next)
    setPage(1)
    if (isLive) setRevision((current) => current + 1)
  }
  const toggleStock = () => {
    setInStock((current) => !current)
    setPage(1)
    if (isLive) setRevision((current) => current + 1)
  }

  const openPublicVerification = async () => {
    if (!IS_TAURI) {
      setVerificationMessage('请在比牌桌面版中完成人真验证。')
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
        setRevision((current) => current + 1)
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

  const resultData = liveQuery.data
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
  const searching = isLive && liveQuery.isFetching
  const challenged = Boolean(
    resultData?.warnings?.some((warning) =>
      /滑块|验证页|网页验证|访问保护冷却|站点保护冷却|库存(?:暂)?无法确认|库存暂不可确认|未能完成.*核验|未返回可核验商品/.test(
        warning,
      ),
    ),
  )
  const title = activeShortcutInfo?.label || (keyword ? `“${keyword}”` : '实时商品搜索')
  const retailStatus =
    typeof resultData?.retail_index?.indexed_shops === 'number'
      ? resultData.retail_index
      : retailIndexQuery.data
  const pickaiStatus =
    typeof resultData?.pickai_index?.product_count === 'number'
      ? resultData.pickai_index
      : pickaiIndexQuery.data

  useEffect(() => {
    if (resultData && page > pages) changePage(pages)
    // changePage intentionally excluded to avoid recreating this correction effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pages, resultData])

  return (
    <div className="flex h-full min-h-0 bg-[var(--page)]">
      <aside className="flex w-[218px] shrink-0 flex-col border-r border-[var(--border)] bg-[var(--panel)]">
        <div className="px-4 pb-3 pt-5">
          <div className="text-[10.5px] font-bold uppercase tracking-[0.12em] text-[var(--soft)]">
            快捷分类
          </div>
          <p className="mt-1 text-[11.5px] leading-5 text-[var(--muted)]">点击后立即发起实时搜索</p>
        </div>

        <nav className="space-y-1 px-2.5" aria-label="商品快捷分类">
          {SHORTCUTS.map((item) => {
            const selected = item.id === activeShortcut
            const Icon = item.icon
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => startSearch(item.query)}
                aria-current={selected ? 'page' : undefined}
                className={clsx(
                  'category-item group flex w-full items-center gap-3 rounded-lg px-3 py-3 text-left outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-ring)]',
                  selected
                    ? 'bg-[var(--brand-soft)] text-[var(--brand-strong)]'
                    : 'text-[var(--text)] hover:bg-[var(--surface)]',
                )}
              >
                <span
                  className={clsx(
                    'grid h-8 w-8 shrink-0 place-items-center rounded-lg border',
                    selected
                      ? 'border-[color-mix(in_srgb,var(--brand)_24%,transparent)] bg-white text-[var(--brand)]'
                      : 'border-[var(--border)] bg-white text-[var(--soft)] group-hover:text-[var(--text)]',
                  )}
                >
                  <Icon size={16} strokeWidth={2} />
                </span>
                <span className="min-w-0">
                  <span className="block truncate text-[13px] font-semibold">{item.label}</span>
                  <span className="mt-0.5 block truncate text-[10.5px] text-[var(--soft)]">
                    {item.detail}
                  </span>
                </span>
                {selected && <i className="ml-auto h-5 w-[2px] rounded-full bg-[var(--brand)]" />}
              </button>
            )
          })}
        </nav>

        <div className="mx-4 mt-5 rounded-lg border border-[var(--border)] bg-[var(--panel-soft)] p-3">
          <div className="flex items-center gap-2 text-[11.5px] font-semibold text-[var(--ink)]">
            <ShieldCheck size={14} className="text-[var(--brand)]" />
            实时结果规则
          </div>
          <p className="mt-1.5 text-[10.5px] leading-[18px] text-[var(--muted)]">
            PickAI 和本地索引只负责选店。价格、库存只认本轮原店响应。
          </p>
        </div>

        <div className="mt-auto">
          <SourceSummary retail={retailStatus} pickai={pickaiStatus} />
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex h-[72px] shrink-0 items-center border-b border-[var(--border)] bg-white px-5">
          <div className="flex h-11 min-w-0 flex-1 items-center rounded-[10px] border border-[var(--border-strong)] bg-[var(--panel-soft)] focus-within:border-[var(--brand)] focus-within:ring-[3px] focus-within:ring-[var(--brand-ring)]">
            <Search size={18} className="ml-3.5 shrink-0 text-[var(--soft)]" strokeWidth={2} />
            <input
              ref={searchInput}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              onKeyDown={(event) => event.key === 'Enter' && commit()}
              placeholder="搜索 K12、ChatGPT Plus、邮箱或 OpenAI 接码"
              className="h-full min-w-0 flex-1 bg-transparent px-3 text-[13.5px] text-[var(--ink)] outline-none placeholder:text-[var(--placeholder)]"
            />
            <kbd className="mr-2 hidden rounded border border-[var(--border)] bg-white px-1.5 py-0.5 font-sans text-[10px] text-[var(--soft)] xl:block">
              Ctrl K
            </kbd>
            <button
              type="button"
              onClick={commit}
              disabled={!search.trim() || searching}
              className="ui-control mr-1 inline-flex h-9 items-center gap-1.5 rounded-lg bg-[var(--brand)] px-4 text-[12.5px] font-semibold text-white hover:bg-[var(--brand-strong)] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {searching ? (
                <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" />
              ) : (
                <Search size={14} />
              )}
              搜索
            </button>
          </div>
        </header>

        <div className="flex min-h-0 flex-1 flex-col px-5 pb-5 pt-4">
          <div className="flex shrink-0 items-end justify-between gap-4">
            <div className="min-w-0">
              <div className="flex min-w-0 items-center gap-2.5">
                <h1 className="truncate text-[20px] font-bold tracking-[-0.025em] text-[var(--ink)]">
                  {title}
                </h1>
                {isLive && strictRealtimeSearch && (
                  <span className="inline-flex shrink-0 items-center gap-1 rounded-md bg-[var(--success-bg)] px-2 py-1 text-[10.5px] font-semibold text-[var(--success-text)]">
                    <ShieldCheck size={11} />
                    严格实时
                  </span>
                )}
              </div>
              <div className="mt-1 flex items-center gap-2 text-[11.5px] text-[var(--muted)]">
                {!isLive ? (
                  <span>选一个分类，或在上方输入关键词</span>
                ) : searching ? (
                  <span className="inline-flex items-center gap-1.5 text-[var(--brand)]">
                    <LoaderCircle size={12} className="animate-spin motion-reduce:animate-none" />
                    正在并行核验多家低价原店，首轮通常 4–8 秒
                  </span>
                ) : liveQuery.isError ? (
                  <span className="text-[var(--danger-text)]">本次实时搜索失败</span>
                ) : challenged ? (
                  <span className="inline-flex items-center gap-1.5 text-[var(--warning-text)]">
                    <AlertTriangle size={12} />
                    原店未完成核验，旧数据已隐藏
                  </span>
                ) : (
                  <>
                    <span className="inline-flex items-center gap-1.5 text-[var(--success-text)]">
                      <i className="h-1.5 w-1.5 rounded-full bg-current" />
                      本轮核验完成
                    </span>
                    <span>共 {total.toLocaleString('zh-CN')} 条</span>
                    {lowestPrice !== null && <span>最低 {cny(lowestPrice)}</span>}
                    {resultData?.refreshed_at && (
                      <span className="inline-flex items-center gap-1">
                        <Clock3 size={11} />
                        {relTime(resultData.refreshed_at)}
                      </span>
                    )}
                  </>
                )}
              </div>
            </div>

            <div className="flex shrink-0 items-center gap-2">
              <select
                value={platform}
                onChange={(event) => {
                  setPlatform(event.target.value as Platform)
                  setPage(1)
                  if (isLive) setRevision((current) => current + 1)
                }}
                aria-label="搜索来源"
                className="h-8 rounded-lg border border-[var(--border)] bg-white px-2 text-[11.5px] font-medium text-[var(--text)] outline-none focus:border-[var(--brand)]"
              >
                <option value="all">全部来源</option>
                <option value="ldxp">链动小铺</option>
                <option value="catfk">云猫寄售</option>
              </select>
              <div className="flex rounded-lg border border-[var(--border)] bg-white p-0.5">
                <button
                  type="button"
                  onClick={() => changeSort('sale_asc')}
                  className={clsx(
                    'h-7 rounded-md px-2.5 text-[11.5px] font-semibold',
                    sort === 'sale_asc'
                      ? 'bg-[var(--surface-selected)] text-[var(--ink)]'
                      : 'text-[var(--soft)] hover:text-[var(--text)]',
                  )}
                >
                  最低价
                </button>
                <button
                  type="button"
                  onClick={() => changeSort('stock_desc')}
                  className={clsx(
                    'h-7 rounded-md px-2.5 text-[11.5px] font-semibold',
                    sort === 'stock_desc'
                      ? 'bg-[var(--surface-selected)] text-[var(--ink)]'
                      : 'text-[var(--soft)] hover:text-[var(--text)]',
                  )}
                >
                  库存
                </button>
              </div>
              <button
                type="button"
                onClick={toggleStock}
                aria-pressed={inStock}
                className={clsx(
                  'ui-control inline-flex h-8 items-center gap-1.5 rounded-lg border px-2.5 text-[11.5px] font-semibold',
                  inStock
                    ? 'border-[color-mix(in_srgb,var(--brand)_28%,var(--border))] bg-[var(--brand-soft)] text-[var(--brand-strong)]'
                    : 'border-[var(--border)] bg-white text-[var(--soft)]',
                )}
              >
                <CheckCircle2 size={13} />
                仅看有货
              </button>
              {isLive && !searching && (
                <button
                  type="button"
                  onClick={() => setRevision((current) => current + 1)}
                  aria-label="重新实时核验"
                  title="重新实时核验"
                  className="ui-control grid h-8 w-8 place-items-center rounded-lg border border-[var(--border)] bg-white text-[var(--soft)] hover:border-[var(--brand)] hover:text-[var(--brand)]"
                >
                  <RefreshCw size={14} />
                </button>
              )}
            </div>
          </div>

          {isLive && !searching && Boolean(resultData?.warnings?.length) && (
            <div className="mt-3 flex shrink-0 items-start gap-2 rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] px-3 py-2 text-[11.5px] leading-5 text-[var(--warning-text)]">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" />
              <span className="min-w-0 flex-1">
                {resultData?.warnings?.join('；')}
                {challenged && ' 已隐藏所有未核验的旧价格和延迟库存。'}
                {challenged && verificationMessage && (
                  <span className="ml-1 font-semibold">{verificationMessage}</span>
                )}
              </span>
              {challenged && (
                <button
                  type="button"
                  onClick={() => void openPublicVerification()}
                  disabled={verificationPending}
                  className="ui-control inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md border border-[var(--warning-border)] bg-white px-3 font-semibold text-[var(--warning-text)] shadow-sm hover:border-[var(--warning-text)] disabled:cursor-wait disabled:opacity-65"
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
                    className="shrink-0 font-semibold underline underline-offset-2"
                  >
                    连接设置
                  </button>
                )}
            </div>
          )}

          <div className="mt-3 flex min-h-0 flex-1 flex-col overflow-hidden rounded-[10px] border border-[var(--border)] bg-white">
            <div
              className={clsx(
                'grid h-10 shrink-0 items-center gap-4 border-b border-[var(--border)] bg-[var(--table-head)] px-5 text-[10.5px] font-bold uppercase tracking-[0.06em] text-[var(--soft)]',
                GRID,
              )}
            >
              <div>商品与商家</div>
              <div className="text-right">当前售价</div>
              <div>原店库存</div>
              <div className="text-right">操作</div>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto">
              {!isLive ? (
                <InitialState onSearch={startSearch} />
              ) : searching ? (
                <LoadingRows />
              ) : liveQuery.isError ? (
                <div className="flex min-h-[330px] items-center justify-center px-8 text-center">
                  <div>
                    <AlertTriangle className="mx-auto text-[var(--danger-text)]" size={22} />
                    <h3 className="mt-3 text-[15px] font-semibold text-[var(--ink)]">实时搜索失败</h3>
                    <p className="mt-1 text-[12px] text-[var(--muted)]">
                      没有展示缓存结果。点击右上角刷新后重新核验。
                    </p>
                  </div>
                </div>
              ) : items.length === 0 ? (
                <NoResults challenged={challenged} />
              ) : (
                items.map((product) => (
                  <ProductRow key={product.id} product={product} onOpen={setActive} />
                ))
              )}
            </div>

            {isLive && !searching && total > 0 && (
              <footer className="flex h-12 shrink-0 items-center justify-between border-t border-[var(--border)] px-5 text-[11.5px] text-[var(--soft)]">
                <span>
                  第 {page} / {pages} 页，本页 {items.length} 条已核验
                </span>
                <div className="flex items-center gap-1.5">
                  <button
                    type="button"
                    disabled={page <= 1}
                    onClick={() => changePage(page - 1)}
                    className="ui-control inline-flex h-8 items-center gap-1 rounded-lg border border-[var(--border)] bg-white px-2.5 font-semibold text-[var(--text)] disabled:opacity-35"
                  >
                    <ChevronLeft size={13} />
                    上一页
                  </button>
                  <button
                    type="button"
                    disabled={page >= pages}
                    onClick={() => changePage(page + 1)}
                    className="ui-control inline-flex h-8 items-center gap-1 rounded-lg border border-[var(--border)] bg-white px-2.5 font-semibold text-[var(--text)] disabled:opacity-35"
                  >
                    下一页
                    <ChevronRight size={13} />
                  </button>
                </div>
              </footer>
            )}
          </div>
        </div>
      </section>

      {active && (
        <Suspense fallback={null}>
          <ProductDrawer product={active} onClose={() => setActive(null)} />
        </Suspense>
      )}
    </div>
  )
}
