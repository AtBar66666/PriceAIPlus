type ToastType = 'info' | 'success' | 'error'

export function toast(message: string, type: ToastType = 'info') {
  let c = document.getElementById('pa-toast')
  if (!c) {
    c = document.createElement('div')
    c.id = 'pa-toast'
    c.style.cssText =
      'position:fixed;top:18px;left:50%;transform:translateX(-50%);z-index:2000;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none'
    document.body.appendChild(c)
  }
  const bg = type === 'success' ? '#2f7a4b' : type === 'error' ? '#9b3328' : '#2d3435'
  const el = document.createElement('div')
  el.textContent = message
  el.style.cssText = `padding:9px 18px;border-radius:999px;background:${bg};color:#f8f8f8;font-size:13px;font-weight:500;box-shadow:0 30px 80px rgba(45,52,53,.18);opacity:0;transform:translateY(-8px);transition:opacity .2s,transform .2s`
  c.appendChild(el)
  requestAnimationFrame(() => {
    el.style.opacity = '1'
    el.style.transform = 'none'
  })
  setTimeout(() => {
    el.style.opacity = '0'
    el.style.transform = 'translateY(-8px)'
    setTimeout(() => el.remove(), 240)
  }, 2200)
}
