import { useCallback, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import clsx from 'clsx'
import {
  AlertCircle,
  LoaderCircle,
  LogIn,
  Search,
  Settings as SettingsIcon,
  Store,
} from 'lucide-react'
import { TitleBar } from './components/TitleBar'
import { Dot } from './components/ui'
import { api, isTokenFailure } from './lib/api'
import { toast } from './lib/toast'
import { Products } from './views/Products'
import { RetailShops } from './views/RetailShops'
import { Settings } from './views/Settings'

type ViewId = 'products' | 'shops' | 'settings'

const NAV = [
  { key: 'products' as const, label: '商品搜索', icon: Search },
  { key: 'shops' as const, label: '零售店铺', icon: Store },
  { key: 'settings' as const, label: '连接设置', icon: SettingsIcon },
]

const isTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

function isRetailIndexRunning(status?: { running?: boolean; state?: string }): boolean {
  return Boolean(
    status?.running || status?.state === 'discovering' || status?.state === 'indexing',
  )
}

export default function App() {
  const qc = useQueryClient()
  const [view, setView] = useState<ViewId>('products')
  const [keyword, setKeyword] = useState('')
  const [searchSeq, setSearchSeq] = useState(0)

  const requestSearch = useCallback((query: string) => {
    const next = query.trim()
    if (!next) return
    setKeyword(next)
    setView('products')
    setSearchSeq((seq) => seq + 1)
  }, [])

  const loginLdxp = useMutation({
    mutationFn: async () => {
      if (!isTauri) throw new Error('请在比牌桌面版中使用自动登录')
      await invoke('open_ldxp_login')
    },
    onSuccess: () => toast('登录窗口已打开，完成登录后可直接搜索实时货源', 'success'),
    onError: (error) =>
      toast(error instanceof Error ? error.message : '无法打开登录窗口', 'error'),
  })
  const loginCatfk = useMutation({
    mutationFn: async () => {
      if (!isTauri) throw new Error('请在比牌桌面版中使用自动登录')
      await invoke('open_catfk_login')
    },
    onSuccess: () => toast('登录窗口已打开，完成登录后可直接搜索实时货源', 'success'),
    onError: (error) =>
      toast(error instanceof Error ? error.message : '无法打开登录窗口', 'error'),
  })

  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const ldxpConnection = useQuery({
    queryKey: ['connection-test', 'ldxp'],
    queryFn: () => api.testConnection('ldxp'),
    enabled: false,
    retry: false,
  })
  const catfkConnection = useQuery({
    queryKey: ['connection-test', 'catfk'],
    queryFn: () => api.testConnection('catfk'),
    enabled: false,
    retry: false,
  })
  const retailIndex = useQuery({
    queryKey: ['retail-index'],
    queryFn: api.retailIndex,
    retry: false,
    refetchInterval: (query) => (isRetailIndexRunning(query.state.data) ? 3_000 : false),
  })
  const pickaiIndex = useQuery({
    queryKey: ['pickai-index'],
    queryFn: api.pickaiIndex,
    retry: false,
    refetchInterval: (query) => (query.state.data?.running ? 2_000 : false),
  })

  const ldxpTokenFailed = isTokenFailure(ldxpConnection.data)
  const catfkTokenFailed = isTokenFailure(catfkConnection.data)
  const tokenFailed = ldxpTokenFailed || catfkTokenFailed
  const hasConfiguredToken = Boolean(
    settings.data?.has_token || settings.data?.has_catfk_token,
  )
  const connectionChecking =
    hasConfiguredToken &&
    ((Boolean(settings.data?.has_token) && ldxpConnection.isFetching) ||
      (Boolean(settings.data?.has_catfk_token) && catfkConnection.isFetching))
  const connectionError = ldxpConnection.isError || catfkConnection.isError
  const connected = Boolean(ldxpConnection.data?.ok || catfkConnection.data?.ok)
  const connectionLabel = tokenFailed
    ? '登录令牌已失效'
    : connectionChecking
      ? '正在验证登录令牌'
      : connectionError
        ? '连接验证失败'
        : connected
          ? '货源池已连接'
          : hasConfiguredToken
            ? '登录令牌已配置'
            : '公开免登录模式'
  const connectionTone: 'ok' | 'bad' | 'warn' | 'muted' = tokenFailed
    ? 'bad'
    : connected
      ? 'ok'
      : hasConfiguredToken
        ? 'warn'
        : 'muted'
  const failedPlatformLabel =
    ldxpTokenFailed && catfkTokenFailed
      ? '链动小铺和云猫寄售'
      : ldxpTokenFailed
        ? '链动小铺'
        : '云猫寄售'

  const retailRunning = isRetailIndexRunning(retailIndex.data)

  useEffect(() => {
    if (!isTauri) return
    let disposed = false
    let unlisteners: UnlistenFn[] = []
    void Promise.all([
      listen<{ message: string }>('ldxp-token-captured', (event) => {
        toast(event.payload.message, 'success')
        void qc.invalidateQueries({ queryKey: ['settings'] })
        void qc.invalidateQueries({ queryKey: ['connection-test', 'ldxp'] })
      }),
      listen<{ message: string }>('ldxp-token-capture-error', (event) => {
        toast(event.payload.message, 'error')
      }),
      listen<{ message: string }>('ldxp-token-capture-progress', (event) => {
        toast(event.payload.message, 'success')
      }),
      listen<{ message: string }>('catfk-token-captured', (event) => {
        toast(event.payload.message, 'success')
        void qc.invalidateQueries({ queryKey: ['settings'] })
        void qc.invalidateQueries({ queryKey: ['connection-test', 'catfk'] })
      }),
      listen<{ message: string }>('catfk-token-capture-error', (event) => {
        toast(event.payload.message, 'error')
      }),
      listen<{ message: string }>('catfk-token-capture-progress', (event) => {
        toast(event.payload.message, 'success')
      }),
      listen('backend-ready', () => {
        void qc.invalidateQueries()
      }),
      listen<{ message: string }>('backend-error', (event) => {
        toast(event.payload.message || '本地服务启动失败，请重启应用', 'error')
      }),
    ]).then((registered) => {
      if (disposed) registered.forEach((unlisten) => unlisten())
      else unlisteners = registered
    })
    return () => {
      disposed = true
      unlisteners.forEach((unlisten) => unlisten())
    }
  }, [qc])

  const retailCount =
    typeof retailIndex.data?.indexed_shops === 'number'
      ? retailIndex.data.indexed_shops.toLocaleString('zh-CN')
      : '…'
  const pickaiCount =
    typeof pickaiIndex.data?.product_count === 'number'
      ? pickaiIndex.data.product_count.toLocaleString('zh-CN')
      : '…'
  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-[var(--bone)]">
      <TitleBar />

      <header className="flex shrink-0 items-end justify-between gap-6 border-b-2 border-[var(--rule)] px-10 pb-0 pt-3">
        <div className="flex min-w-0 items-end gap-12">
          <div className="flex items-baseline gap-2.5 pb-3">
            <span className="text-[22px] font-extrabold leading-none tracking-[-0.04em] text-[var(--ink)]">
              比牌
            </span>
            <span className="num text-[11px] font-semibold leading-none tracking-[0.08em] text-[var(--faint)]">
              BIPAI
            </span>
          </div>
          <nav className="flex items-end gap-8" aria-label="主导航">
            {NAV.map((item) => {
              const active = view === item.key
              return (
                <button
                  key={item.key}
                  type="button"
                  onClick={() => setView(item.key)}
                  aria-current={active ? 'page' : undefined}
                  className={clsx(
                    'ui-control relative pb-3 text-[15.5px] leading-none',
                    active
                      ? 'font-bold text-[var(--ink)] after:absolute after:bottom-[-2px] after:left-0 after:h-[2px] after:w-full after:bg-[var(--blue)]'
                      : 'font-medium text-[var(--muted)] hover:text-[var(--ink)]',
                  )}
                >
                  {item.label}
                </button>
              )
            })}
          </nav>
        </div>
        <button
          type="button"
          onClick={() => setView('settings')}
          title="打开连接设置"
          className="ui-control flex shrink-0 items-center gap-2 pb-3 text-[13.5px] font-medium text-[var(--muted)] hover:text-[var(--ink)]"
        >
          <Dot tone={connectionTone} />
          {connectionLabel}
        </button>
      </header>

      <main className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {tokenFailed && (
          <div className="flex h-11 shrink-0 items-center gap-2.5 border-b border-[var(--bad-border)] bg-[var(--bad-bg)] px-10 text-[13.5px] text-[var(--bad)]">
            <AlertCircle size={15} className="shrink-0" />
            <span className="min-w-0 flex-1">
              {failedPlatformLabel} Merchant-Token 已失效，官方全局搜索结果会不完整。
            </span>
            <button
              type="button"
              onClick={() =>
                ldxpTokenFailed
                  ? loginLdxp.mutate()
                  : catfkTokenFailed
                    ? loginCatfk.mutate()
                    : setView('settings')
              }
              disabled={loginLdxp.isPending || loginCatfk.isPending}
              className="ui-control inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md border border-[var(--bad-border)] bg-[var(--card)] px-3 text-[13px] font-medium text-[var(--bad)] hover:bg-[var(--bad-bg)]"
            >
              {loginLdxp.isPending || loginCatfk.isPending ? (
                <LoaderCircle size={13} className="animate-spin" />
              ) : (
                <LogIn size={13} />
              )}
              {loginLdxp.isPending || loginCatfk.isPending
                ? '正在打开'
                : ldxpTokenFailed
                  ? '重新登录链动'
                  : '重新登录云猫'}
            </button>
          </div>
        )}
        <div
          className={clsx(
            'min-h-0 flex-1',
            view === 'products' ? 'overflow-hidden' : 'overflow-y-auto px-10 py-9',
          )}
        >
          {view === 'products' && (
            <Products
              keyword={keyword}
              searchSeq={searchSeq}
              onSearch={requestSearch}
              onOpenSettings={() => setView('settings')}
            />
          )}
          {view === 'shops' && <RetailShops />}
          {view === 'settings' && (
            <Settings
              onLoginLdxp={() => loginLdxp.mutate()}
              ldxpLoginPending={loginLdxp.isPending}
              onLoginCatfk={() => loginCatfk.mutate()}
              catfkLoginPending={loginCatfk.isPending}
            />
          )}
        </div>
      </main>

      <footer className="flex h-9 shrink-0 items-center justify-between gap-6 border-t border-[var(--border)] px-10 text-[12.5px] text-[var(--faint)]">
        <div className="flex min-w-0 items-center gap-7">
          <span className="tnum whitespace-nowrap">
            零售店 <span className="num font-semibold text-[var(--muted)]">{retailCount}</span> 家
            {retailRunning ? '（索引中）' : ''}
          </span>
          <span className="tnum whitespace-nowrap">
            PickAI 分类 <span className="num font-semibold text-[var(--muted)]">{pickaiCount}</span> 条
          </span>
        </div>
        <span className="hidden truncate lg:block">价格与库存只认原店本轮实时响应</span>
      </footer>
    </div>
  )
}
