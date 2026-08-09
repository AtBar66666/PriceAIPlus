import type { ButtonHTMLAttributes, ReactNode } from 'react'
import clsx from 'clsx'
import { Package } from 'lucide-react'

type Variant = 'dark' | 'soft' | 'ghost' | 'outline' | 'danger'
type Size = 'sm' | 'md'

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant
  size?: Size
}

const VARIANT: Record<Variant, string> = {
  dark:
    'border border-transparent bg-[var(--primary)] text-[var(--on-primary)] shadow-[var(--shadow-pop)] hover:border-[color-mix(in_srgb,var(--brand)_45%,var(--primary))] hover:bg-[var(--primary-hover)] hover:shadow-[0_2px_5px_rgb(20_26_27_/_0.18),0_10px_24px_rgb(44_117_72_/_0.16)]',
  soft:
    'border border-transparent bg-[var(--surface)] text-[var(--ink)] hover:border-[color-mix(in_srgb,var(--brand)_34%,var(--border))] hover:bg-[var(--brand-soft)] hover:text-[var(--success-text)] hover:shadow-[var(--shadow-hover)]',
  ghost:
    'border border-transparent text-[var(--muted)] hover:border-[color-mix(in_srgb,var(--brand)_28%,var(--border))] hover:bg-[var(--brand-soft)] hover:text-[var(--success-text)] hover:shadow-[var(--shadow-hover)]',
  outline:
    'border border-[var(--border)] bg-white text-[var(--ink)] shadow-[var(--shadow-xs)] hover:border-[color-mix(in_srgb,var(--brand)_42%,var(--border))] hover:bg-[var(--brand-soft)] hover:text-[var(--success-text)] hover:shadow-[var(--shadow-hover)]',
  danger:
    'border border-transparent bg-[var(--danger-bg)] text-[var(--danger-text)] hover:border-[color-mix(in_srgb,var(--danger-text)_24%,transparent)] hover:bg-[#f6ddd8] hover:shadow-[0_7px_18px_rgb(155_51_40_/_0.12)]',
}

export function Button({ variant = 'soft', size = 'md', className, ...p }: BtnProps) {
  return (
    <button
      {...p}
      className={clsx(
        'ui-control inline-flex select-none items-center justify-center gap-2 whitespace-nowrap rounded-full font-medium disabled:pointer-events-none disabled:opacity-40 disabled:shadow-none',
        size === 'sm' ? 'h-9 px-4 text-[13px]' : 'h-11 px-6 text-[14px]',
        VARIANT[variant],
        className,
      )}
    />
  )
}

export function IconButton({
  className,
  tone = 'outline',
  ...p
}: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: 'outline' | 'ghost' }) {
  return (
    <button
      {...p}
      className={clsx(
        'ui-control inline-flex h-11 w-11 items-center justify-center rounded-full disabled:pointer-events-none disabled:opacity-40 disabled:shadow-none',
        tone === 'outline'
          ? 'border border-[var(--border)] bg-white text-[var(--muted)] shadow-[var(--shadow-xs)] hover:border-[color-mix(in_srgb,var(--brand)_42%,var(--border))] hover:bg-[var(--brand-soft)] hover:text-[var(--success-text)] hover:shadow-[var(--shadow-hover)]'
          : 'border border-transparent text-[var(--soft)] hover:border-[color-mix(in_srgb,var(--brand)_28%,var(--border))] hover:bg-[var(--brand-soft)] hover:text-[var(--success-text)] hover:shadow-[var(--shadow-hover)]',
        className,
      )}
    />
  )
}

export function Switch({ checked, onChange }: { checked: boolean; onChange: () => void }) {
  return (
    <button
      type="button"
      onClick={onChange}
      role="switch"
      aria-checked={checked}
      className="ui-control group flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full p-[2px] hover:shadow-[0_0_0_4px_rgb(55_171_106_/_0.13)] focus:outline-none"
      style={{ background: checked ? 'var(--brand)' : '#d6dcdd' }}
    >
      <span
        aria-hidden
        className={clsx(
          'block h-5 w-5 rounded-full bg-white shadow-[0_1px_2px_rgba(20,26,27,0.18),0_0_0_1px_rgba(20,26,27,0.04)] transition-[transform,box-shadow] duration-200 ease-out group-hover:shadow-[0_2px_5px_rgba(20,26,27,0.24),0_0_0_1px_rgba(20,26,27,0.05)] motion-reduce:transition-none',
          checked ? 'translate-x-[20px]' : 'translate-x-0',
        )}
      />
    </button>
  )
}

export function Spinner() {
  return (
    <div className="mx-auto my-14 h-7 w-7 animate-spin rounded-full border-[2.5px] border-[var(--border)] border-t-[var(--brand)]" />
  )
}

/** 统一的页面标题区：大标题 + 说明 + 可选的次要统计行（三页一致） */
export function PageHeader({ title, desc, stats }: { title: string; desc: ReactNode; stats?: ReactNode }) {
  return (
    <div className="mb-7">
      <h1 className="t-display">{title}</h1>
      <p className="mt-2 max-w-3xl text-[14px] leading-6 text-[var(--muted)]">{desc}</p>
      {stats && <div className="mt-3.5">{stats}</div>}
    </div>
  )
}

export function EmptyState({ text }: { text: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-3 py-16 text-center">
      <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-[var(--surface)] text-[var(--soft)]">
        <Package size={20} />
      </div>
      <div className="max-w-xs text-[13.5px] leading-relaxed text-[var(--soft)]">{text}</div>
    </div>
  )
}

export function StockPill({ stock, status }: { stock: number; status?: string }) {
  if (status === '未上架')
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-[var(--surface)] px-3 py-1.5 text-[12.5px] font-medium text-[var(--soft)]">
        <i className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
        未上架
      </span>
    )
  if (status === '缺货')
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-[var(--danger-bg)] px-3 py-1.5 text-[12.5px] font-medium text-[var(--danger-text)]">
        <i className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
        缺货
      </span>
    )
  if (stock < 0)
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-[var(--surface)] px-3 py-1.5 text-[12.5px] font-medium text-[var(--soft)]">
        <i className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
        库存未知
      </span>
    )
  if (stock === 0)
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-[var(--danger-bg)] px-3 py-1.5 text-[12.5px] font-medium text-[var(--danger-text)]">
        <i className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
        缺货
      </span>
    )
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-[var(--success-bg)] px-3 py-1.5 text-[12.5px] font-medium tabular-nums text-[var(--success-text)]">
      <i className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
      有货 {stock.toLocaleString('zh-CN')}
    </span>
  )
}

export function StatusTag({ label, tone }: { label: string; tone: 'ok' | 'error' | 'warn' | 'muted' }) {
  const map = {
    ok: 'bg-[var(--success-bg)] text-[var(--success-text)]',
    error: 'bg-[var(--danger-bg)] text-[var(--danger-text)]',
    warn: 'bg-[var(--warning-bg)] text-[var(--warning-text)]',
    muted: 'bg-[var(--surface)] text-[var(--soft)]',
  }
  const dot = {
    ok: 'bg-[var(--success-text)]',
    error: 'bg-[var(--danger-text)]',
    warn: 'bg-[var(--warning-text)]',
    muted: 'bg-[var(--soft)]',
  }
  return (
    <span className={clsx('inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[12.5px] font-medium', map[tone])}>
      <i className={clsx('h-1.5 w-1.5 rounded-full opacity-80', dot[tone])} />
      {label}
    </span>
  )
}
