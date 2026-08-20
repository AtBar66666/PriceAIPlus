type ToastType = 'info' | 'success' | 'error'

const DOT: Record<ToastType, string> = {
  info: '#a1a1aa',
  success: '#10b981',
  error: '#f87171',
}

export function toast(message: string, type: ToastType = 'info') {
  let c = document.getElementById('pa-toast')
  if (!c) {
    c = document.createElement('div')
    c.id = 'pa-toast'
    c.style.cssText =
      'position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:2000;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none'
    document.body.appendChild(c)
  }
  const el = document.createElement('div')
  el.style.cssText =
    'display:flex;align-items:center;gap:8px;max-width:min(520px,86vw);padding:8px 14px;border-radius:8px;background:#18181b;color:#fafafa;font-size:12.5px;font-weight:500;box-shadow:0 8px 24px rgba(0,0,0,.18);opacity:0;transform:translateY(-6px);transition:opacity .18s,transform .18s'
  const dot = document.createElement('i')
  dot.style.cssText = `flex-shrink:0;width:6px;height:6px;border-radius:999px;background:${DOT[type]}`
  const text = document.createElement('span')
  text.textContent = message
  el.append(dot, text)
  c.appendChild(el)
  requestAnimationFrame(() => {
    el.style.opacity = '1'
    el.style.transform = 'none'
  })
  setTimeout(() => {
    el.style.opacity = '0'
    el.style.transform = 'translateY(-6px)'
    setTimeout(() => el.remove(), 220)
  }, 2400)
}
