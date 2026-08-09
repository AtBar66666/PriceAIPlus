import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Eye, EyeOff, KeyRound, LoaderCircle, LogIn, Plug, Save, ShieldCheck, Trash2 } from 'lucide-react'
import { api, isTokenFailure, type TestResult } from '../lib/api'
import { toast } from '../lib/toast'
import { Button, PageHeader, StatusTag } from '../components/ui'

const STEPS = [
  { t: '浏览器登录 ldxp.cn 商家后台，按 F12 打开开发者工具', d: '' },
  { t: '方式A：Application → Local Storage → https://www.ldxp.cn', d: '找到 key 为 auth-token 的项，复制里面 value 字段那串（不含引号）' },
  { t: '方式B：Network → 任一 merchantApi 请求 → 请求头', d: '复制 Merchant-Token 这一行的值' },
  { t: '粘贴到下面 → 保存 → 点「测试连接」', d: '' },
]

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

  const input =
    'ring-focus h-11 w-full rounded-xl border border-[var(--border)] bg-white px-4 font-mono text-[13.5px] text-[var(--text)] shadow-[var(--shadow-xs)] outline-none transition-[border-color,box-shadow] duration-200 ease-out placeholder:font-sans placeholder:text-[var(--placeholder)] hover:border-[color-mix(in_srgb,var(--brand)_34%,var(--border))] hover:shadow-[var(--shadow-hover)]'
  const card = 'overflow-hidden rounded-[20px] border border-[var(--border-subtle)] bg-white shadow-[var(--shadow-card)]'
  const tokenPlaceholder = s?.has_token
    ? `已保存 ${s.token_preview}，粘贴新值可替换`
    : '粘贴 auth-token 的 value'
  const catfkTokenPlaceholder = s?.has_catfk_token
    ? `已保存 ${s.catfk_token_preview}，粘贴新值可替换`
    : '粘贴 catfk.com 的 auth-token value'
  const delay = s
    ? `${(s.min_delay_ms / 1000).toFixed(1)} - ${(s.max_delay_ms / 1000).toFixed(1)} 秒`
    : '0.7 - 1.6 秒'

  return (
    <div className="max-w-[1000px]">
      <PageHeader title="设置" desc="公开店铺搜索无需登录；账号令牌只用于你主动选择的官方货源功能。" />

      <div className={card}>
        <div className="grid lg:grid-cols-[minmax(0,1fr)_320px]">
          <section className="p-7">
            <div className="flex items-start gap-3.5">
              <div className="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-[var(--brand-soft)] text-[var(--success-text)]">
                <KeyRound size={19} />
              </div>
              <div>
                <h2 className="t-section">平台登录令牌 <span className="font-normal text-[var(--soft)]">（可选）</span></h2>
                <p className="mt-1.5 max-w-xl text-[13.5px] leading-6 text-[var(--muted)]">
                  不登录也能使用 PickAI 发现入口和 pay.ldxp.cn 公开原店接口。
                  Merchant-Token 只为官方账号货源池准备；担心账号风控就保持未配置。
                </p>
              </div>
            </div>

            <div className="mt-6 flex flex-wrap items-center gap-3">
              {!s?.has_token ? (
                <StatusTag label="链动免登录模式" tone="ok" />
              ) : ldxpConnection.isFetching ? (
                <StatusTag label="链动正在验证" tone="muted" />
              ) : ldxpInvalid ? (
                <StatusTag label="链动 Token 已失效" tone="error" />
              ) : ldxpConnection.data?.ok ? (
                <StatusTag label={`链动已连接 ${s.token_preview}`} tone="ok" />
              ) : (
                <StatusTag label={`链动待验证 ${s.token_preview}`} tone="warn" />
              )}
              {!s?.has_catfk_token ? (
                <StatusTag label="云猫未配置" tone="warn" />
              ) : catfkConnection.isFetching ? (
                <StatusTag label="云猫正在验证" tone="muted" />
              ) : catfkInvalid ? (
                <StatusTag label="云猫 Token 已失效" tone="error" />
              ) : catfkConnection.data?.ok ? (
                <StatusTag label={`云猫已连接 ${s.catfk_token_preview}`} tone="ok" />
              ) : (
                <StatusTag label={`云猫待验证 ${s.catfk_token_preview}`} tone="warn" />
              )}
              {s?.has_cookie ? <StatusTag label="Cookie 已配置" tone="ok" /> : <StatusTag label="Cookie 未配置（可选）" tone="muted" />}
            </div>

            <div className="mt-6 flex flex-col gap-4 rounded-2xl border border-[color-mix(in_srgb,var(--brand)_24%,var(--border-subtle))] bg-[var(--brand-soft)] p-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex min-w-0 items-start gap-3">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-white text-[var(--success-text)] shadow-[var(--shadow-xs)]">
                  <LogIn size={17} />
                </div>
                <div>
                  <div className="text-[13.5px] font-semibold text-[var(--ink)]">自动获取链动 Token</div>
                  <p className="mt-1 text-[12px] leading-5 text-[var(--muted)]">
                    在独立窗口完成官网登录或验证码，成功后自动验证、保存并关闭窗口。
                  </p>
                </div>
              </div>
              <Button
                className="group shrink-0"
                variant="dark"
                disabled={ldxpLoginPending}
                onClick={onLoginLdxp}
              >
                {ldxpLoginPending ? (
                  <LoaderCircle size={16} className="animate-spin" />
                ) : (
                  <LogIn size={16} className="ui-icon-motion transition-transform duration-200 group-hover:translate-x-0.5" />
                )}
                {ldxpLoginPending ? '正在打开' : s?.has_token ? '重新登录链动' : '登录链动（可选）'}
              </Button>
            </div>

            <label className="mt-6 block text-[13px] font-semibold text-[var(--text)]" htmlFor="merchant-token">
              链动小铺 Merchant-Token
            </label>
            <div className="relative mt-2">
              <input
                id="merchant-token"
                type={showToken ? 'text' : 'password'}
                autoComplete="off"
                className={`${input} pr-11`}
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder={tokenPlaceholder}
              />
              <button
                type="button"
                onClick={() => setShowToken((value) => !value)}
                aria-label={showToken ? '隐藏令牌' : '显示令牌'}
                className="group absolute right-1.5 top-1/2 inline-flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-lg border border-transparent text-[var(--soft)] transition-[color,background-color,border-color,box-shadow,scale] duration-200 ease-out hover:border-[color-mix(in_srgb,var(--brand)_32%,var(--border))] hover:bg-[var(--brand-soft)] hover:text-[var(--success-text)] hover:shadow-[var(--shadow-xs)] active:scale-95 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[color-mix(in_srgb,var(--brand)_52%,transparent)] motion-reduce:transition-none motion-reduce:active:scale-100"
              >
                {showToken ? (
                  <EyeOff size={16} className="ui-icon-motion transition-transform duration-200 group-hover:scale-110" />
                ) : (
                  <Eye size={16} className="ui-icon-motion transition-transform duration-200 group-hover:scale-110" />
                )}
              </button>
            </div>
            <p className="mt-2 text-[12px] leading-5 text-[var(--soft)]">
              已有令牌时留空不会覆盖；需要更新时再粘贴新值。
            </p>

            <div className="mt-6 flex flex-col gap-4 rounded-2xl border border-[color-mix(in_srgb,var(--brand)_24%,var(--border-subtle))] bg-[var(--brand-soft)] p-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex min-w-0 items-start gap-3">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-white text-[var(--success-text)] shadow-[var(--shadow-xs)]">
                  <LogIn size={17} />
                </div>
                <div>
                  <div className="text-[13.5px] font-semibold text-[var(--ink)]">自动获取云猫 Token</div>
                  <p className="mt-1 text-[12px] leading-5 text-[var(--muted)]">
                    在独立窗口完成 catfk.com 登录，成功后自动验证、保存并关闭窗口。
                  </p>
                </div>
              </div>
              <Button
                className="group shrink-0"
                variant="dark"
                disabled={catfkLoginPending}
                onClick={onLoginCatfk}
              >
                {catfkLoginPending ? (
                  <LoaderCircle size={16} className="animate-spin" />
                ) : (
                  <LogIn size={16} className="ui-icon-motion transition-transform duration-200 group-hover:translate-x-0.5" />
                )}
                {catfkLoginPending ? '正在打开' : s?.has_catfk_token ? '重新登录云猫' : '登录云猫'}
              </Button>
            </div>

            <label className="mt-5 block text-[13px] font-semibold text-[var(--text)]" htmlFor="catfk-merchant-token">
              云猫寄售 Merchant-Token
            </label>
            <div className="relative mt-2">
              <input
                id="catfk-merchant-token"
                type={showCatfkToken ? 'text' : 'password'}
                autoComplete="off"
                className={`${input} pr-11`}
                value={catfkToken}
                onChange={(e) => setCatfkToken(e.target.value)}
                placeholder={catfkTokenPlaceholder}
              />
              <button
                type="button"
                onClick={() => setShowCatfkToken((value) => !value)}
                aria-label={showCatfkToken ? '隐藏云猫令牌' : '显示云猫令牌'}
                className="group absolute right-1.5 top-1/2 inline-flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-lg border border-transparent text-[var(--soft)] transition-[color,background-color,border-color,box-shadow,scale] duration-200 ease-out hover:border-[color-mix(in_srgb,var(--brand)_32%,var(--border))] hover:bg-[var(--brand-soft)] hover:text-[var(--success-text)] hover:shadow-[var(--shadow-xs)] active:scale-95 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[color-mix(in_srgb,var(--brand)_52%,transparent)] motion-reduce:transition-none motion-reduce:active:scale-100"
              >
                {showCatfkToken ? (
                  <EyeOff size={16} className="ui-icon-motion transition-transform duration-200 group-hover:scale-110" />
                ) : (
                  <Eye size={16} className="ui-icon-motion transition-transform duration-200 group-hover:scale-110" />
                )}
              </button>
            </div>

            <label className="mt-5 block text-[13px] font-semibold text-[var(--text)]" htmlFor="merchant-cookie">
              Cookie <span className="font-normal text-[var(--soft)]">（可选）</span>
            </label>
            <input
              id="merchant-cookie"
              type="password"
              autoComplete="off"
              className={`${input} mt-2`}
              value={cookie}
              onChange={(e) => setCookie(e.target.value)}
              placeholder={s?.has_cookie ? 'Cookie 已保存，粘贴新值可替换' : '一般不需要，可留空'}
            />

            <div className="mt-6 flex flex-wrap gap-3">
              <Button className="group" variant="dark" disabled={(!token.trim() && !catfkToken.trim() && !cookie.trim()) || save.isPending} onClick={() => save.mutate()}>
                <Save size={16} className="ui-icon-motion transition-transform duration-200 group-hover:-translate-y-0.5" /> 保存凭据
              </Button>
              <Button className="group" variant="outline" disabled={!s?.has_token || runTest.isPending} onClick={() => runTest.mutate('ldxp')}>
                <Plug size={16} className="ui-icon-motion transition-transform duration-200 group-hover:translate-x-0.5" /> {runTest.isPending && runTest.variables === 'ldxp' ? '正在测试' : '测试链动'}
              </Button>
              <Button className="group" variant="outline" disabled={!s?.has_catfk_token || runTest.isPending} onClick={() => runTest.mutate('catfk')}>
                <Plug size={16} className="ui-icon-motion transition-transform duration-200 group-hover:translate-x-0.5" /> {runTest.isPending && runTest.variables === 'catfk' ? '正在测试' : '测试云猫'}
              </Button>
              <Button
                className="group"
                variant="danger"
                disabled={(!s?.has_token && !s?.has_cookie) || clearLdxp.isPending}
                onClick={() => {
                  if (window.confirm('删除本机保存的链动 Token 与 Cookie，并切回公开免登录模式？')) {
                    clearLdxp.mutate()
                  }
                }}
              >
                {clearLdxp.isPending ? (
                  <LoaderCircle size={16} className="animate-spin" />
                ) : (
                  <Trash2 size={16} />
                )}
                {clearLdxp.isPending ? '正在清除' : '清除链动凭据'}
              </Button>
            </div>

            {test && (
              <div
                className="mt-5 rounded-xl border px-4 py-3.5"
                style={{
                  background: test.ok ? 'var(--success-bg)' : 'var(--danger-bg)',
                  borderColor: test.ok ? 'rgba(44,117,72,0.22)' : 'rgba(155,51,40,0.22)',
                }}
              >
                <div className="text-[13px] font-semibold" style={{ color: test.ok ? 'var(--success-text)' : 'var(--danger-text)' }}>
                  {test.message}
                </div>
                {(test.sample_keys?.length || test.preview) && (
                  <details className="mt-2 text-[12px] text-[var(--muted)]">
                    <summary className="cursor-pointer select-none font-medium transition-colors duration-200 hover:text-[var(--success-text)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color-mix(in_srgb,var(--brand)_52%,transparent)]">
                      查看响应详情
                    </summary>
                    {test.sample_keys && test.sample_keys.length > 0 && (
                      <div className="mt-2.5 flex flex-wrap gap-1.5">
                        {test.sample_keys.map((key) => (
                          <span key={key} className="rounded-md bg-white/70 px-2 py-0.5 font-mono text-[11px]">
                            {key}
                          </span>
                        ))}
                      </div>
                    )}
                    {test.preview && (
                      <pre className="mt-2.5 max-h-44 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-white/70 p-3 font-mono text-[11px] leading-relaxed text-[var(--text)]">
                        {test.preview}
                      </pre>
                    )}
                  </details>
                )}
              </div>
            )}
          </section>

          <aside className="border-t border-[var(--border-subtle)] bg-[var(--panel-soft)] p-7 lg:border-l lg:border-t-0">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-[var(--border-subtle)] bg-white text-[var(--muted)] shadow-[var(--shadow-xs)]">
              <ShieldCheck size={19} />
            </div>
            <h3 className="mt-4 text-[15px] font-semibold text-[var(--ink)]">自动登录失败时</h3>
            <p className="mt-2 text-[12px] leading-5 text-[var(--soft)]">
              一般无需手动复制。只有官网页面结构变化时，才使用下面的备用方式。
            </p>
            <ol className="mt-5 space-y-4">
              {STEPS.map((step, index) => (
                <li key={step.t} className="flex gap-3">
                  <span className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-white text-[11px] font-semibold text-[var(--muted)] shadow-[var(--shadow-xs)]">
                    {index + 1}
                  </span>
                  <div>
                    <div className="text-[12.5px] font-medium leading-5 text-[var(--text)]">{step.t}</div>
                    {step.d && <div className="mt-1 text-[11.5px] leading-5 text-[var(--soft)]">{step.d}</div>}
                  </div>
                </li>
              ))}
            </ol>
          </aside>
        </div>
      </div>

      <div className={`${card} mt-6 p-7`}>
        <div>
          <h2 className="t-section">抓取参数</h2>
          <p className="mt-1.5 text-[13px] text-[var(--soft)]">当前运行配置，仅在本机生效。</p>
        </div>
        <div className="mt-5 grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-[var(--border-subtle)] bg-[var(--border-subtle)] sm:grid-cols-4">
          {[
            { k: '目标站点', v: s ? 'ldxp.cn + catfk.com' : '未读取' },
            { k: '浏览器指纹', v: s?.impersonate ?? '未读取' },
            { k: '请求间隔', v: delay },
            { k: '并发', v: s ? `搜索 6 / 索引 ${s.retail_index_concurrency}` : '未读取' },
          ].map((c) => (
            <div key={c.k} className="bg-white px-4 py-4">
              <div className="t-label">{c.k}</div>
              <div className="mt-1.5 truncate text-[14px] font-semibold text-[var(--ink)]">{c.v}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
