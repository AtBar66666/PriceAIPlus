import type { ButtonHTMLAttributes, ReactNode } from 'react'
import clsx from 'clsx'

type Variant = 'primary' | 'outline' | 'ghost' | 'danger'
type Size = 'sm' | 'md' | 'lg'

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant
  size?: Size
}

const VARIANT: Record<Variant, string> = {
  primary:
    'border border-transparent bg-[var(--ink)] text-[var(--bone)] hover:bg-[var(--blue)]',
  outline:
    'border border-[var(--border-2)] bg-transparent text-[var(--text)] hover:border-[var(--ink)] hover:text-[var(--ink)]',
  ghost:
    'border border-transparent text-[var(--muted)] hover:bg-[var(--surface)] hover:text-[var(--ink)]',
  danger:
    'border border-[var(--bad-border)] bg-transparent text-[var(--bad)] hover:bg-[var(--bad-bg)]',
}

const SIZE: Record<Size, string> = {
  sm: 'h-8 px-3 text-[13px]',
  md: 'h-9 px-3.5 text-[14px]',
  lg: 'h-10 px-4 text-[14.5px]',
}

export function Button({ variant = 'outline', size = 'md', className, ...p }: BtnProps) {
  return (
    <button
      {...p}
      className={clsx(
        'ui-control inline-flex select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-[6px] font-medium disabled:pointer-events-none disabled:opacity-40',
        SIZE[size],
        VARIANT[variant],
        className,
      )}
    />
  )
}

export function IconButton({
  className,
  tone = 'ghost',
  ...p
}: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: 'outline' | 'ghost' | 'danger' }) {
  return (
    <button
      {...p}
      className={clsx(
        'ui-control inline-flex h-9 w-9 items-center justify-center rounded-[6px] disabled:pointer-events-none disabled:opacity-40',
        tone === 'outline' &&
          'border border-[var(--border-2)] bg-transparent text-[var(--muted)] hover:border-[var(--ink)] hover:text-[var(--ink)]',
        tone === 'ghost' &&
          'border border-transparent text-[var(--muted)] hover:bg-[var(--surface)] hover:text-[var(--ink)]',
        tone === 'danger' &&
          'border border-transparent text-[var(--muted)] hover:bg-[var(--bad-bg)] hover:text-[var(--bad)]',
        className,
      )}
    />
  )
}

/** 表单输入：白面 + 边框 + 克莱因蓝聚焦环 */
export const fieldClass =
  'field h-10 w-full rounded-[6px] border border-[var(--border-2)] bg-[var(--card)] px-3.5 text-[14.5px] text-[var(--ink)] outline-none placeholder:text-[var(--placeholder)] disabled:cursor-not-allowed disabled:bg-[var(--surface)] disabled:opacity-60'

export function Switch({ checked, onChange }: { checked: boolean; onChange: () => void }) {
  return (
    <button
      type="button"
      onClick={onChange}
      role="switch"
      aria-checked={checked}
      className="ui-control flex h-[22px] w-10 shrink-0 cursor-pointer items-center rounded-full p-[2px]"
      style={{ background: checked ? 'var(--accent)' : 'var(--border-2)' }}
    >
      <span
        aria-hidden
        className={clsx(
          'block h-[18px] w-[18px] rounded-full bg-white shadow-[0_1px_2px_rgba(15,15,15,0.2)] transition-transform duration-150 ease-out motion-reduce:transition-none',
          checked ? 'translate-x-[18px]' : 'translate-x-0',
        )}
      />
    </button>
  )
}

export function Spinner() {
  return (
    <div className="mx-auto my-14 h-5 w-5 animate-spin rounded-full border-2 border-[var(--border)] border-t-[var(--ink)] motion-reduce:animate-none" />
  )
}

/** 页头：大标题 */
export function PageHeader({
  title,
  desc,
  actions,
}: {
  title: string
  desc?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="mb-7 flex items-start justify-between gap-4">
      <div className="min-w-0">
        <h1 className="t-title">{title}</h1>
        {desc && (
          <p className="mt-1.5 max-w-2xl text-[14px] leading-[1.6] text-[var(--muted)]">{desc}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2 pt-2">{actions}</div>}
    </div>
  )
}

export function EmptyState({ text, children }: { text: ReactNode; children?: ReactNode }) {
  return (
    <div className="px-1 py-16 text-center">
      <div className="mx-auto max-w-md text-[14px] leading-[1.6] text-[var(--muted)]">{text}</div>
      {children && <div className="mt-5 flex justify-center gap-2">{children}</div>}
    </div>
  )
}

type DotTone = 'ok' | 'bad' | 'warn' | 'muted' | 'accent'

const DOT: Record<DotTone, string> = {
  ok: 'var(--ok-dot)',
  bad: 'var(--bad)',
  warn: 'var(--warn)',
  muted: 'var(--faint)',
  accent: 'var(--accent)',
}

export function Dot({ tone }: { tone: DotTone }) {
  return (
    <i
      aria-hidden
      className="inline-block h-[7px] w-[7px] shrink-0 rounded-full"
      style={{ background: DOT[tone] }}
    />
  )
}

export function StatusText({
  label,
  tone,
  className,
}: {
  label: ReactNode
  tone: DotTone
  className?: string
}) {
  const color =
    tone === 'ok'
      ? 'var(--ok)'
      : tone === 'bad'
        ? 'var(--bad)'
        : tone === 'warn'
          ? 'var(--warn)'
          : tone === 'accent'
            ? 'var(--accent)'
            : 'var(--muted)'
  return (
    <span
      className={clsx('inline-flex items-center gap-1.5 text-[13.5px] font-medium', className)}
      style={{ color }}
    >
      <Dot tone={tone} />
      {label}
    </span>
  )
}

/** 库存：Notion 式暖色标签 */
export function StockCell({ stock, status }: { stock: number; status?: string }) {
  if (status === '未上架')
    return <span className="tag bg-[var(--surface)] text-[var(--muted)]">未上架</span>
  if (status === '缺货' || stock === 0)
    return <span className="tag bg-[var(--bad-bg)] text-[var(--bad)]">缺货</span>
  if (stock < 0)
    return <span className="tag bg-[var(--surface)] text-[var(--muted)]">库存未知</span>
  return (
    <span className="tag bg-[var(--ok-bg)] text-[var(--ok)]">
      有货 <span className="num">{stock.toLocaleString('zh-CN')}</span>
    </span>
  )
}
