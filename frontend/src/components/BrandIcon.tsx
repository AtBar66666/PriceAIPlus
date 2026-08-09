import type { ReactNode } from 'react'
import { CATEGORY_COLOR } from '../lib/format'

/**
 * 依据商品名关键词推断平台，展示统一风格的内联品牌图标（离线可用，单色跟随浅底方块）。
 * 匹配优先级：Claude → Gemini/Google → Grok → OpenAI → 邮箱 → 兜底（品类色首字母）
 */

type Brand = { key: string; color: string; glyph: ReactNode }

const OpenAI = (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden>
    <path d="M22.282 9.821a5.985 5.985 0 0 0-.516-4.911 6.046 6.046 0 0 0-6.51-2.9A6.065 6.065 0 0 0 4.981 4.18a5.985 5.985 0 0 0-3.998 2.9 6.046 6.046 0 0 0 .743 7.097 5.98 5.98 0 0 0 .51 4.911 6.051 6.051 0 0 0 6.515 2.9A5.985 5.985 0 0 0 13.26 24a6.056 6.056 0 0 0 5.772-4.206 5.99 5.99 0 0 0 3.998-2.9 6.056 6.056 0 0 0-.747-7.073zM13.26 22.43a4.476 4.476 0 0 1-2.876-1.04l.141-.081 4.779-2.758a.795.795 0 0 0 .392-.681v-6.737l2.02 1.168a.071.071 0 0 1 .038.052v5.583a4.504 4.504 0 0 1-4.494 4.494zM3.6 18.304a4.47 4.47 0 0 1-.535-3.014l.142.085 4.783 2.758a.771.771 0 0 0 .78 0l5.843-3.369v2.332a.08.08 0 0 1-.033.062L9.74 19.95a4.499 4.499 0 0 1-6.14-1.646zM2.34 7.896a4.485 4.485 0 0 1 2.366-1.973V11.6a.766.766 0 0 0 .388.676l5.815 3.355-2.02 1.168a.076.076 0 0 1-.071 0l-4.83-2.786A4.504 4.504 0 0 1 2.34 7.872zm16.597 3.855-5.833-3.387L15.119 7.2a.076.076 0 0 1 .071 0l4.83 2.791a4.494 4.494 0 0 1-.676 8.105v-5.678a.79.79 0 0 0-.407-.667zm2.01-3.023-.141-.085-4.774-2.782a.776.776 0 0 0-.785 0L9.409 9.23V6.897a.066.066 0 0 1 .028-.061l4.83-2.787a4.499 4.499 0 0 1 6.68 4.66zM8.307 12.863l-2.02-1.164a.08.08 0 0 1-.038-.057V6.074a4.499 4.499 0 0 1 7.376-3.454l-.142.08L8.704 5.46a.795.795 0 0 0-.393.681zm1.097-2.365 2.602-1.5 2.607 1.5v2.999l-2.597 1.5-2.607-1.5z" />
  </svg>
)

const Claude = (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden>
    {Array.from({ length: 12 }).map((_, i) => (
      <rect key={i} x="11.05" y="2.4" width="1.9" height="6.1" rx="0.95" transform={`rotate(${i * 30} 12 12)`} />
    ))}
  </svg>
)

const Gemini = (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden>
    <path d="M12 2c.42 5.06 4.94 9.58 10 10-5.06.42-9.58 4.94-10 10-.42-5.06-4.94-9.58-10-10 5.06-.42 9.58-4.94 10-10Z" />
  </svg>
)

const Grok = (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden>
    <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24h-6.66l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25h6.83l4.713 6.231 5.447-6.231Zm-1.161 17.52h1.833L7.084 4.126H5.117l11.966 15.644Z" />
  </svg>
)

const Mail = (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <rect x="3" y="5.5" width="18" height="13" rx="2.6" />
    <path d="M3.8 7.5 12 13l8.2-5.5" />
  </svg>
)

const BRANDS: { test: RegExp; brand: Brand }[] = [
  { test: /claude|anthropic|sonnet|\bopus\b|haiku/i, brand: { key: 'claude', color: '#c9743f', glyph: Claude } },
  { test: /gemini|谷歌|google|bard/i, brand: { key: 'gemini', color: '#4285f4', glyph: Gemini } },
  { test: /grok/i, brand: { key: 'grok', color: '#1b2223', glyph: Grok } },
  { test: /gpt|chatgpt|openai|codex|sub2api|k12|bug\s*team|team\s*bug/i, brand: { key: 'openai', color: '#10a37f', glyph: OpenAI } },
  { test: /邮箱|mail|icloud|outlook|gmail|hotmail|@/i, brand: { key: 'mail', color: '#5f7488', glyph: Mail } },
]

function initial(name?: string): string {
  if (!name) return '#'
  const m = name.match(/[A-Za-z0-9\u4e00-\u9fa5]/)
  return m ? m[0].toUpperCase() : '#'
}

export function BrandIcon({ name, category }: { name: string; category: string }) {
  const hit = BRANDS.find((b) => b.test.test(name))

  if (hit) {
    const c = hit.brand.color
    return (
      <span
        className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[13px]"
        style={{
          color: c,
          background: `color-mix(in srgb, ${c} 13%, white)`,
          boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${c} 20%, transparent)`,
        }}
      >
        {hit.brand.glyph}
      </span>
    )
  }

  const c = CATEGORY_COLOR[category] ?? '#7b8283'
  return (
    <span
      className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[13px] text-[16px] font-bold"
      style={{
        color: c,
        background: `color-mix(in srgb, ${c} 14%, white)`,
        boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${c} 20%, transparent)`,
      }}
    >
      {initial(name)}
    </span>
  )
}
