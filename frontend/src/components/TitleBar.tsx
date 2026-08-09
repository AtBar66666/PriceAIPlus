import { Minus, Square, X } from 'lucide-react'
import { getCurrentWindow } from '@tauri-apps/api/window'

const inTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
const appWin = inTauri ? getCurrentWindow() : null

export function TitleBar() {
  return (
    <div
      data-tauri-drag-region
      className="flex h-8 shrink-0 select-none items-center border-b border-[var(--border)] bg-white"
    >
      <div
        data-tauri-drag-region
        className="flex h-full flex-1 items-center gap-2 pl-3 text-[11px] font-medium text-[var(--soft)]"
      >
        <img src="/app-icons/32x32.png" alt="" className="h-4 w-4 rounded-full object-contain" />
        比牌 / 实时商品搜索
      </div>
      <button
        type="button"
        onClick={() => appWin?.minimize()}
        aria-label="最小化"
        className="group grid h-8 w-[46px] place-items-center text-[var(--muted)] transition-[color,background-color] duration-150 hover:bg-[var(--surface)] hover:text-[var(--ink)] active:bg-[var(--surface-selected)] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[color-mix(in_srgb,var(--brand)_52%,transparent)]"
      >
        <Minus size={15} className="ui-icon-motion transition-transform duration-200 group-hover:translate-y-px" />
      </button>
      <button
        type="button"
        onClick={() => appWin?.toggleMaximize()}
        aria-label="最大化"
        className="group grid h-8 w-[46px] place-items-center text-[var(--muted)] transition-[color,background-color] duration-150 hover:bg-[var(--surface)] hover:text-[var(--ink)] active:bg-[var(--surface-selected)] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[color-mix(in_srgb,var(--brand)_52%,transparent)]"
      >
        <Square size={12} className="ui-icon-motion transition-transform duration-200 group-hover:scale-110" />
      </button>
      <button
        type="button"
        onClick={() => appWin?.close()}
        aria-label="关闭"
        className="group grid h-8 w-[46px] place-items-center text-[var(--muted)] transition-[color,background-color] duration-200 ease-out hover:bg-[#e81123] hover:text-white active:bg-[#c50f1f] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[#e81123]"
      >
        <X size={16} className="ui-icon-motion transition-transform duration-200 group-hover:scale-110" />
      </button>
    </div>
  )
}
