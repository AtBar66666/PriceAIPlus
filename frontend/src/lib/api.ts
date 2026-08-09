const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? 'http://127.0.0.1:8756'

export type Product = {
  id: number
  shop_id: number
  name: string
  category: string
  merchant_name: string
  sale_price: number
  agent_price: number
  cost_price: number
  stock: number
  status: string
  is_linked: boolean
  url: string
  shop_url: string
  source_kind: 'source' | 'retail' | 'pickai'
  platform: 'ldxp' | 'catfk'
  margin: number
  margin_pct: number
  last_seen_at: string
  verified: boolean
  verified_at: string | null
}

export type SearchPlatform = 'all' | 'ldxp' | 'catfk'

export type ProductsResponse = {
  items: Product[]
  total: number
  page: number
  page_size: number
  complete: boolean
}

export type RetailIndexState = 'idle' | 'discovering' | 'indexing' | 'ready' | 'error'

export type RetailIndexStatus = {
  state: RetailIndexState
  running: boolean
  discovered_shops: number
  indexed_shops: number
  pending_shops: number
  failed_shops: number
  changed_shops?: number
  product_count: number
  progress: number
  current_shop: string
  message: string
  started_at: string | null
  finished_at: string | null
  scope: string
  coverage_note: string
  cooldown_seconds?: number
  deferred_seconds?: number
}

export type PickAIIndexStatus = {
  state: 'idle' | 'syncing' | 'ready' | 'error'
  running: boolean
  stale: boolean
  product_count: number
  product_types: number
  completed_types: number
  pages_completed: number
  declared_quotes: number
  progress: number
  current_type: string
  message: string
  error: string
  started_at: string | null
  finished_at: string | null
  last_synced_at: string | null
  json_path: string
  csv_path: string
  categories?: number
  relay_items?: number
  duplicates_merged?: number
  request_count?: number
}

export type LiveSearchResponse = ProductsResponse & {
  retail_index: RetailIndexStatus
  pickai_index: PickAIIndexStatus
  warnings?: string[]
  refreshing?: boolean
  refresh_started?: boolean
  refreshed_at?: string | null
  refresh_error?: string
  verified_count: number
  index_updated_at: string | null
  mode?: 'cache' | 'verify'
  fallback_mode?: 'origin_unavailable' | null
  fallback_items?: Product[]
  fallback_total?: number
}

export type RetailShop = {
  id: number
  name: string
  token: string
  url: string
  product_count: number
  active: boolean
  last_synced_at: string | null
  created_at: string
}

export type RetailShopsResponse = {
  items: RetailShop[]
  total: number
}

async function readResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try {
      const payload = (await response.json()) as { detail?: string; message?: string }
      message = payload.detail || payload.message || message
    } catch {
      // 保留 HTTP 状态作为非 JSON 错误提示。
    }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { signal })
  return readResponse<T>(response)
}

async function send<T>(path: string, method: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  return readResponse<T>(response)
}

export const api = {
  cachedSearch: (params: {
    keywords: string
    goods_type?: string
    in_stock?: boolean
    page?: number
    page_size?: number
    sort?: 'sale_asc' | 'stock_desc'
    platform?: SearchPlatform
  }, signal?: AbortSignal) => {
    const query = new URLSearchParams()
    query.set('keywords', params.keywords)
    if (params.goods_type) query.set('goods_type', params.goods_type)
    if (params.in_stock !== undefined) query.set('in_stock', String(params.in_stock))
    if (params.page !== undefined) query.set('page', String(params.page))
    if (params.page_size !== undefined) query.set('page_size', String(params.page_size))
    if (params.sort) query.set('sort', params.sort)
    if (params.platform) query.set('platform', params.platform)
    return get<LiveSearchResponse>(`/api/cached-search?${query.toString()}`, signal)
  },
  liveSearch: (params: {
    keywords: string
    goods_type?: string
    in_stock?: boolean
    page?: number
    page_size?: number
    sort?: 'sale_asc' | 'stock_desc'
    platform?: SearchPlatform
    public_only?: boolean
  }, signal?: AbortSignal) => {
    const query = new URLSearchParams()
    query.set('keywords', params.keywords)
    if (params.goods_type) query.set('goods_type', params.goods_type)
    if (params.in_stock !== undefined) query.set('in_stock', String(params.in_stock))
    if (params.page !== undefined) query.set('page', String(params.page))
    if (params.page_size !== undefined) query.set('page_size', String(params.page_size))
    if (params.sort) query.set('sort', params.sort)
    if (params.platform) query.set('platform', params.platform)
    if (params.public_only !== undefined) query.set('public_only', String(params.public_only))
    return get<LiveSearchResponse>(`/api/live-search?${query.toString()}`, signal)
  },
  retailIndex: () => get<RetailIndexStatus>('/api/retail-index'),
  refreshRetailIndex: () => send<RetailIndexStatus>('/api/retail-index/refresh', 'POST'),
  pickaiIndex: () => get<PickAIIndexStatus>('/api/pickai-index'),
  refreshPickAIIndex: () => send<PickAIIndexStatus>('/api/pickai-index/refresh', 'POST'),
  retailShops: () => get<RetailShopsResponse>('/api/retail-shops'),
  addRetailShop: (url: string) =>
    send<{ ok: boolean; message: string; shop: RetailShop }>('/api/retail-shops', 'POST', { url }),
  refreshRetailShop: (shopId: number) =>
    send<{ ok: boolean; message: string; shop: RetailShop }>(`/api/retail-shops/${shopId}/refresh`, 'POST'),
  refreshAllRetailShops: () =>
    send<{ ok: boolean; message: string; refreshed: number; total: number; errors: Array<{ shop_id: number; name: string; message: string }> }>(
      '/api/retail-shops/refresh-all',
      'POST',
    ),
  deleteRetailShop: (shopId: number) =>
    send<{ ok: boolean; message: string }>(`/api/retail-shops/${shopId}`, 'DELETE'),
  settings: () => get<SettingsInfo>('/api/settings'),
  saveCredentials: (body: { merchant_token?: string; catfk_merchant_token?: string; cookie?: string }) =>
    send<{ ok: boolean; has_token: boolean; has_catfk_token: boolean; has_cookie: boolean }>('/api/settings', 'PUT', body),
  clearLdxpCredentials: () =>
    send<{ ok: boolean; has_token: boolean; has_cookie: boolean; message: string }>(
      '/api/settings/ldxp-credentials',
      'DELETE',
    ),
  testConnection: (platform: 'ldxp' | 'catfk' = 'ldxp') =>
    send<TestResult>(`/api/test-connection?platform=${platform}`, 'POST'),
}

export type SettingsInfo = {
  has_cookie: boolean
  cookie_preview: string
  has_token: boolean
  token_preview: string
  has_catfk_token: boolean
  catfk_token_preview: string
  has_public_clearance?: boolean
  base_url: string
  catfk_base_url: string
  impersonate: string
  min_delay_ms: number
  max_delay_ms: number
  retail_index_concurrency: number
}

export type TestResult = {
  ok: boolean
  reason?: string
  message: string
  total?: number
  sample_keys?: string[]
  preview?: string
}

export function isTokenFailure(result?: TestResult): boolean {
  if (!result || result.ok) return false
  return (
    result.reason === 'no_token' ||
    result.reason === 'api_error' ||
    /未登录|请先登录|merchant-token|token.*(?:失效|无效)/i.test(result.message)
  )
}
