import { Minus, Square, X } from 'lucide-react'
import { getCurrentWindow } from '@tauri-apps/api/window'

const inTauri = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
const appWin = inTauri ? getCurrentWindow() : null

const controlClass =
  'grid h-9 w-[46px] place-items-center text-[var(--muted)] transition-colors duration-100 hover:bg-[var(--surface)] hover:text-[var(--ink)] active:bg-[var(--surface-2)] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[var(--accent)]'

export function TitleBar() {
  return (
    <div
      data-tauri-drag-region
      className="flex h-9 shrink-0 select-none items-center border-b border-[var(--border-soft)] bg-[var(--bg)]"
    >
      <div
        data-tauri-drag-region
        className="flex h-full flex-1 items-center gap-2 pl-3.5 text-[12px] font-medium text-[var(--muted)]"
      >
        <img src="/app-icons/32x32.png" alt="" className="h-4 w-4 object-contain" />
        比牌
      </div>
      <button type="button" onClick={() => appWin?.minimize()} aria-label="最小化" className={controlClass}>
        <Minus size={15} />
      </button>
      <button type="button" onClick={() => appWin?.toggleMaximize()} aria-label="最大化" className={controlClass}>
        <Square size={11} />
      </button>
      <button
        type="button"
        onClick={() => appWin?.close()}
        aria-label="关闭"
        className="grid h-9 w-[46px] place-items-center text-[var(--muted)] transition-colors duration-100 hover:bg-[#e81123] hover:text-white active:bg-[#c50f1f] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[#e81123]"
      >
        <X size={15} />
      </button>
    </div>
  )
}
