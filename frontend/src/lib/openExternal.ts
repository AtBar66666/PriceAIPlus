import { isTauri } from '@tauri-apps/api/core'
import { openUrl } from '@tauri-apps/plugin-opener'

export async function openExternal(rawUrl: string): Promise<void> {
  const url = new URL(rawUrl)
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    throw new Error('不支持的链接协议')
  }

  if (isTauri()) {
    await openUrl(url.href)
    return
  }

  window.open(url.href, '_blank', 'noopener,noreferrer')
}
