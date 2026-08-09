import { useEffect, useState } from 'react'
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

export default function App() {
  const qc = useQueryClient()
  const [view, setView] = useState<ViewId>('products')
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
  const failedPlatformLabel =
    ldxpTokenFailed && catfkTokenFailed
      ? '链动小铺和云猫寄售'
      : ldxpTokenFailed
        ? '链动小铺'
        : '云猫寄售'

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
    ]).then((registered) => {
      if (disposed) registered.forEach((unlisten) => unlisten())
      else unlisteners = registered
    })
    return () => {
      disposed = true
      unlisteners.forEach((unlisten) => unlisten())
    }
  }, [qc])

  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-[var(--page)]">
      <TitleBar />

      <div className="flex flex-1 overflow-hidden">
        <aside className="flex w-[68px] shrink-0 flex-col bg-[var(--rail)]" aria-label="主导航">
          <div className="grid h-[66px] place-items-center border-b border-white/8">
            <img
              src="/app-icons/128x128@2x.png"
              alt="比牌"
              className="h-9 w-9 rounded-[10px] object-contain"
            />
          </div>

          <nav className="mt-3 flex flex-col items-center gap-1.5 px-2">
            {NAV.map((item) => {
              const active = view === item.key
              const Icon = item.icon
              return (
                <button
                  key={item.key}
                  type="button"
                  onClick={() => setView(item.key)}
                  aria-label={item.label}
                  title={item.label}
                  className={clsx(
                    'ui-control grid h-11 w-11 place-items-center rounded-[10px]',
                    active
                      ? 'bg-[var(--brand)] text-white'
                      : 'text-white/55 hover:bg-white/8 hover:text-white',
                  )}
                >
                  <Icon size={18} strokeWidth={active ? 2.2 : 1.9} />
                </button>
              )
            })}
          </nav>

          <div className="mt-auto flex justify-center border-t border-white/8 py-4">
            <button
              type="button"
              onClick={() => setView('settings')}
              aria-label={connectionLabel}
              title={connectionLabel}
              className="ui-control grid h-9 w-9 place-items-center rounded-lg hover:bg-white/8"
            >
              <i
                className="h-2.5 w-2.5 rounded-full ring-4 ring-white/5"
                style={{
                  background: tokenFailed
                    ? 'var(--danger-text)'
                    : connected
                      ? 'var(--success-text)'
                      : 'var(--warning-text)',
                }}
              />
            </button>
          </div>
        </aside>

        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          {tokenFailed && (
            <div className="flex h-10 shrink-0 items-center gap-2.5 border-b border-[var(--danger-border)] bg-[var(--danger-bg)] px-4 text-[11.5px] text-[var(--danger-text)]">
              <AlertCircle size={14} className="shrink-0" />
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
                className="ui-control shrink-0 rounded-md border border-[var(--danger-border)] bg-white px-2.5 py-1 text-[11px] font-semibold text-[var(--danger-text)]"
              >
                <span className="inline-flex items-center gap-2">
                  {loginLdxp.isPending || loginCatfk.isPending ? (
                    <LoaderCircle size={15} className="animate-spin" />
                  ) : (
                    <LogIn size={15} />
                  )}
                  {loginLdxp.isPending || loginCatfk.isPending
                    ? '正在打开'
                    : ldxpTokenFailed
                      ? '重新登录链动'
                      : '重新登录云猫'}
                </span>
              </button>
            </div>
          )}
          <div
            className={clsx(
              'min-h-0 flex-1',
              view === 'products' ? 'overflow-hidden' : 'overflow-y-auto px-8 py-8',
            )}
          >
            {view === 'products' && <Products onOpenSettings={() => setView('settings')} />}
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
      </div>
    </div>
  )
}
