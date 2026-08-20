import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Cat,
  Eye,
  EyeOff,
  LifeBuoy,
  LoaderCircle,
  LogIn,
  SlidersHorizontal,
  Store,
  Trash2,
  type LucideIcon,
} from 'lucide-react'
import clsx from 'clsx'
import { api, isTokenFailure, type TestResult } from '../lib/api'
import { toast } from '../lib/toast'
import { Button, fieldClass, PageHeader, StatusText } from '../components/ui'

const STEPS = [
  { t: '浏览器登录 ldxp.cn 商家后台，按 F12 打开开发者工具', d: '' },
  {
    t: '方式A：Application → Local Storage → https://www.ldxp.cn',
    d: '找到 key 为 auth-token 的项，复制里面 value 字段那串（不含引号）',
  },
  { t: '方式B：Network → 任一 merchantApi 请求 → 请求头', d: '复制 Merchant-Token 这一行的值' },
  { t: '粘贴到上面的输入框 → 保存 → 点「测试连接」', d: '' },
]

function SectionHeader({
  icon: Icon,
  title,
  hint,
}: {
  icon: LucideIcon
  title: string
  hint?: string
}) {
  return (
    <div className="flex items-center gap-2.5">
      <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-[var(--border-soft)] bg-[var(--surface)] text-[var(--text)]">
        <Icon size={15} strokeWidth={2} />
      </span>
      <h2 className="t-section">
        {title}
        {hint && (
          <span className="num ml-2 text-[13.5px] font-normal tracking-normal text-[var(--muted)]">
            {hint}
          </span>
        )}
      </h2>
    </div>
  )
}

export function Settings({
  onLoginLdxp,
  ldxpLoginPending = false,
  onLoginCatfk,
  catfkLoginPending = false,
}: {
  onLoginLdxp: () => void
  ldxpLoginPending?: boolean
  onLoginCatfk: () => void
  catfkLoginPending?: boolean
}) {
  const qc = useQueryClient()
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
  const [token, setToken] = useState('')
  const [catfkToken, setCatfkToken] = useState('')
  const [cookie, setCookie] = useState('')
  const [showToken, setShowToken] = useState(false)
  const [showCatfkToken, setShowCatfkToken] = useState(false)
  const [test, setTest] = useState<TestResult | null>(null)

  const save = useMutation({
    mutationFn: () =>
      api.saveCredentials({
        merchant_token: token.trim() || undefined,
        catfk_merchant_token: catfkToken.trim() || undefined,
        cookie: cookie.trim() || undefined,
      }),
    onSuccess: () => {
      toast('凭据已保存', 'success')
      setToken('')
      setCatfkToken('')
      setCookie('')
      qc.invalidateQueries({ queryKey: ['settings'] })
      qc.invalidateQueries({ queryKey: ['connection-test'] })
    },
    onError: () => toast('保存失败，请确认本地服务已启动', 'error'),
  })
  const runTest = useMutation({
    mutationFn: (platform: 'ldxp' | 'catfk') => api.testConnection(platform),
    onSuccess: (r, platform) => {
      setTest(r)
      qc.setQueryData(['connection-test', platform], r)
      toast(r.ok ? '连接成功' : '连接失败', r.ok ? 'success' : 'error')
    },
    onError: () => toast('测试请求失败', 'error'),
  })
  const clearLdxp = useMutation({
    mutationFn: api.clearLdxpCredentials,
    onSuccess: async (result) => {
      setToken('')
      setCookie('')
      setTest(null)
      toast(result.message, 'success')
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['settings'] }),
        qc.invalidateQueries({ queryKey: ['connection-test', 'ldxp'] }),
      ])
    },
    onError: () => toast('清除链动凭据失败', 'error'),
  })

  const s = settings.data
  const ldxpInvalid = isTokenFailure(ldxpConnection.data)
  const catfkInvalid = isTokenFailure(catfkConnection.data)

  const tokenPlaceholder = s?.has_token
    ? `已保存 ${s.token_preview}，粘贴新值可替换`
    : '粘贴 auth-token 的 value'
  const catfkTokenPlaceholder = s?.has_catfk_token
    ? `已保存 ${s.catfk_token_preview}，粘贴新值可替换`
    : '粘贴 catfk.com 的 auth-token value'
  const delay = s
    ? `${(s.min_delay_ms / 1000).toFixed(1)} - ${(s.max_delay_ms / 1000).toFixed(1)} 秒`
    : '读取中'

  const eyeButton =
    'ui-control absolute right-1.5 top-1/2 inline-flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-full text-[var(--faint)] hover:bg-[var(--surface-2)] hover:text-[var(--ink)]'

  const ldxpStatus = !s?.has_token ? (
    <StatusText label="免登录模式，可直接搜索公开店铺" tone="ok" />
  ) : ldxpConnection.isFetching ? (
    <StatusText label="正在验证" tone="muted" />
  ) : ldxpInvalid ? (
    <StatusText label="Token 已失效" tone="bad" />
  ) : ldxpConnection.data?.ok ? (
    <StatusText label={`已连接 ${s.token_preview}`} tone="ok" />
  ) : (
    <StatusText label={`已保存 ${s.token_preview}，尚未验证`} tone="warn" />
  )

  const catfkStatus = !s?.has_catfk_token ? (
    <StatusText label="未配置" tone="muted" />
  ) : catfkConnection.isFetching ? (
    <StatusText label="正在验证" tone="muted" />
  ) : catfkInvalid ? (
    <StatusText label="Token 已失效" tone="bad" />
  ) : catfkConnection.data?.ok ? (
    <StatusText label={`已连接 ${s.catfk_token_preview}`} tone="ok" />
  ) : (
    <StatusText label={`已保存 ${s.catfk_token_preview}，尚未验证`} tone="warn" />
  )

  return (
    <div className="mx-auto max-w-[720px]">
      <PageHeader
        title="设置"
        desc="公开店铺搜索无需登录；账号令牌只用于你主动选择的官方货源功能，担心账号风控就保持未配置。"
      />

      <section className="border-t-2 border-[var(--rule)] pt-7">
        <SectionHeader icon={Store} title="链动小铺" hint="ldxp.cn" />
        <div className="mt-3.5 flex flex-wrap items-center justify-between gap-3">
          {ldxpStatus}
          <div className="flex gap-2">
            <Button type="button" disabled={ldxpLoginPending} onClick={onLoginLdxp}>
              {ldxpLoginPending ? (
                <LoaderCircle size={13} className="animate-spin" />
              ) : (
                <LogIn size={13} />
              )}
              {ldxpLoginPending ? '正在打开' : s?.has_token ? '重新登录' : '自动登录'}
            </Button>
            <Button
              type="button"
              disabled={!s?.has_token || runTest.isPending}
              onClick={() => runTest.mutate('ldxp')}
            >
              {runTest.isPending && runTest.variables === 'ldxp' ? '正在测试' : '测试连接'}
            </Button>
          </div>
        </div>
        <p className="mt-2 text-[13.5px] leading-[1.6] text-[var(--muted)]">
          自动登录会打开独立窗口完成官网登录或验证码，成功后自动验证、保存并关闭。
        </p>

        <label
          className="mt-5 block text-[14px] font-medium text-[var(--text)]"
          htmlFor="merchant-token"
        >
          Merchant-Token
        </label>
        <div className="relative mt-1.5">
          <input
            id="merchant-token"
            type={showToken ? 'text' : 'password'}
            autoComplete="off"
            className={clsx(fieldClass, 'pr-9 font-mono placeholder:font-sans')}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={tokenPlaceholder}
          />
          <button
            type="button"
            onClick={() => setShowToken((value) => !value)}
            aria-label={showToken ? '隐藏令牌' : '显示令牌'}
            className={eyeButton}
          >
            {showToken ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
        </div>
        <p className="mt-2 text-[13px] leading-[1.6] text-[var(--faint)]">
          已有令牌时留空不会覆盖；需要更新时再粘贴新值。
        </p>
      </section>

      <section className="mt-12 border-t-2 border-[var(--rule)] pt-7">
        <SectionHeader icon={Cat} title="云猫寄售" hint="catfk.com" />
        <div className="mt-3.5 flex flex-wrap items-center justify-between gap-3">
          {catfkStatus}
          <div className="flex gap-2">
            <Button type="button" disabled={catfkLoginPending} onClick={onLoginCatfk}>
              {catfkLoginPending ? (
                <LoaderCircle size={13} className="animate-spin" />
              ) : (
                <LogIn size={13} />
              )}
              {catfkLoginPending ? '正在打开' : s?.has_catfk_token ? '重新登录' : '自动登录'}
            </Button>
            <Button
              type="button"
              disabled={!s?.has_catfk_token || runTest.isPending}
              onClick={() => runTest.mutate('catfk')}
            >
              {runTest.isPending && runTest.variables === 'catfk' ? '正在测试' : '测试连接'}
            </Button>
          </div>
        </div>

        <label
          className="mt-5 block text-[14px] font-medium text-[var(--text)]"
          htmlFor="catfk-merchant-token"
        >
          Merchant-Token
        </label>
        <div className="relative mt-1.5">
          <input
            id="catfk-merchant-token"
            type={showCatfkToken ? 'text' : 'password'}
            autoComplete="off"
            className={clsx(fieldClass, 'pr-9 font-mono placeholder:font-sans')}
            value={catfkToken}
            onChange={(e) => setCatfkToken(e.target.value)}
            placeholder={catfkTokenPlaceholder}
          />
          <button
            type="button"
            onClick={() => setShowCatfkToken((value) => !value)}
            aria-label={showCatfkToken ? '隐藏云猫令牌' : '显示云猫令牌'}
            className={eyeButton}
          >
            {showCatfkToken ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
        </div>

        <label
          className="mt-5 block text-[14px] font-medium text-[var(--text)]"
          htmlFor="merchant-cookie"
        >
          Cookie <span className="font-normal text-[var(--faint)]">（可选，一般不需要）</span>
        </label>
        <input
          id="merchant-cookie"
          type="password"
          autoComplete="off"
          className={clsx(fieldClass, 'mt-1.5 font-mono placeholder:font-sans')}
          value={cookie}
          onChange={(e) => setCookie(e.target.value)}
          placeholder={s?.has_cookie ? 'Cookie 已保存，粘贴新值可替换' : '可留空'}
        />

        <div className="mt-5 flex flex-wrap gap-2">
          <Button
            type="button"
            variant="primary"
            disabled={(!token.trim() && !catfkToken.trim() && !cookie.trim()) || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? '正在保存' : '保存凭据'}
          </Button>
          <Button
            type="button"
            variant="danger"
            disabled={(!s?.has_token && !s?.has_cookie) || clearLdxp.isPending}
            onClick={() => {
              if (window.confirm('删除本机保存的链动 Token 与 Cookie，并切回公开免登录模式？')) {
                clearLdxp.mutate()
              }
            }}
          >
            {clearLdxp.isPending ? (
              <LoaderCircle size={13} className="animate-spin" />
            ) : (
              <Trash2 size={13} />
            )}
            {clearLdxp.isPending ? '正在清除' : '清除链动凭据'}
          </Button>
        </div>

        {test && (
          <div
            className={clsx(
              'mt-4 rounded-md border px-3.5 py-3',
              test.ok
                ? 'border-[var(--border)] bg-[var(--head)]'
                : 'border-[var(--bad-border)] bg-[var(--bad-bg)]',
            )}
          >
            <StatusText label={test.message} tone={test.ok ? 'ok' : 'bad'} />
            {(test.sample_keys?.length || test.preview) && (
              <details className="mt-2 text-[12px] text-[var(--muted)]">
                <summary className="cursor-pointer select-none font-medium hover:text-[var(--ink)]">
                  查看响应详情
                </summary>
                {test.sample_keys && test.sample_keys.length > 0 && (
                  <div className="mt-2 font-mono text-[11.5px] leading-5">
                    {test.sample_keys.join('  ')}
                  </div>
                )}
                {test.preview && (
                  <pre className="mt-2 max-h-44 overflow-auto whitespace-pre-wrap break-all rounded-md border border-[var(--border)] bg-[var(--surface)] p-2.5 font-mono text-[11px] leading-relaxed text-[var(--text)]">
                    {test.preview}
                  </pre>
                )}
              </details>
            )}
          </div>
        )}
      </section>

      <section className="mt-12 border-t-2 border-[var(--rule)] pt-7">
        <SectionHeader icon={LifeBuoy} title="自动登录失败时" />
        <p className="mt-3 text-[13.5px] leading-[1.6] text-[var(--muted)]">
          一般无需手动复制，只有官网页面结构变化时才用下面的备用方式。
        </p>
        <ol className="mt-3 space-y-2.5">
          {STEPS.map((step, index) => (
            <li key={step.t} className="flex gap-3 text-[14px] leading-[1.6]">
              <span className="tnum shrink-0 text-[var(--faint)]">{index + 1}.</span>
              <div>
                <div className="text-[var(--text)]">{step.t}</div>
                {step.d && <div className="mt-1 text-[13px] text-[var(--muted)]">{step.d}</div>}
              </div>
            </li>
          ))}
        </ol>
      </section>

      <section className="mb-4 mt-12 border-t-2 border-[var(--rule)] pt-7">
        <SectionHeader icon={SlidersHorizontal} title="抓取参数" />
        <p className="mt-3 text-[13.5px] leading-[1.6] text-[var(--muted)]">当前运行配置，仅在本机生效。</p>
        <dl className="mt-3 space-y-2">
          {[
            { k: '目标站点', v: s ? 'ldxp.cn + catfk.com' : '读取中' },
            { k: '浏览器指纹', v: s?.impersonate ?? '读取中' },
            { k: '请求间隔', v: delay },
            { k: '并发', v: s ? `搜索 6 / 索引 ${s.retail_index_concurrency}` : '读取中' },
          ].map((c) => (
            <div key={c.k} className="flex items-baseline justify-between gap-6 text-[14px]">
              <dt className="text-[var(--muted)]">{c.k}</dt>
              <dd className="num font-semibold text-[var(--ink)]">{c.v}</dd>
            </div>
          ))}
        </dl>
      </section>
    </div>
  )
}
