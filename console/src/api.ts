import { parsePage, parseRelease, parseSources } from './types.ts'
import type { PackageType, ReadResult, Release, Sources } from './types.ts'

export class CatalogError extends Error {
  status: number
  constructor(code: string, status = 0) { super(code); this.status = status }
}
type Fetch = typeof fetch
interface Cached { data: unknown; etag: string | null; checkedAt: number; bytes: number }
const JSON_LIMIT = 4 * 1024 * 1024
async function bounded(response: Response, limit: number): Promise<Uint8Array> {
  const length = response.headers.get('Content-Length')
  if (length && Number(length) > limit) throw new CatalogError('RESPONSE_TOO_LARGE')
  if (!response.body) return new Uint8Array()
  const reader = response.body.getReader(), parts: Uint8Array[] = []
  let size = 0
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      size += value.length
      if (size > limit) throw new CatalogError('RESPONSE_TOO_LARGE')
      parts.push(value)
    }
  } finally { await reader.cancel(); reader.releaseLock() }
  const bytes = new Uint8Array(size)
  let offset = 0
  for (const part of parts) { bytes.set(part, offset); offset += part.length }
  return bytes
}
export function catalogUrl(q: string, type: PackageType | '', offset: number): string {
  const params = new URLSearchParams({ q, offset: String(offset), limit: '18' })
  if (type) params.set('type', type)
  return `/catalog/v1/packages?${params}`
}
export function versionsUrl(namespace: string, id: string, offset = 0, version = ''): string {
  const params = new URLSearchParams({ offset: String(offset), limit: '20' })
  if (version) params.set('version', version)
  return `/catalog/v1/packages/${encodeURIComponent(namespace)}/${encodeURIComponent(id)}/releases?${params}`
}
export function detailUrl(release: Pick<Release, 'namespace' | 'package_id' | 'version'>): string {
  return `/packages/${encodeURIComponent(release.namespace)}/${encodeURIComponent(release.package_id)}?${new URLSearchParams({ version: release.version })}`
}
export function availability(release: Release, sources: Sources | null, fresh = true): string | null {
  if (release.withdrawn) return '发行已撤回'
  if (!fresh) return '正在显示离线缓存，请连接后重新确认'
  if (!sources) return '来源状态尚未确认'
  const key = sources.keys.find(item => item.key_id === release.key_id && item.namespace === release.namespace)
  if (!key) return '签名公钥已移除或未登记'
  if (key.revoked) return '签名公钥已撤销'
  return null
}
export class CatalogClient {
  private cache = new Map<string, Cached>()
  private fetcher: Fetch
  constructor(fetcher: Fetch = fetch) { this.fetcher = fetcher.bind(globalThis) }
  private async request<T>(url: string, parse: (value: unknown) => T, options = { cache: true, fallback: true }): Promise<ReadResult<T>> {
    const saved = options.cache ? this.cache.get(url) : undefined
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000)
    try {
      const response = await this.fetcher(url, { credentials: 'omit', cache: 'no-store', signal: controller.signal, headers: saved?.etag ? { 'If-None-Match': saved.etag } : {} })
      if (response.status === 304) {
        if (!saved || !saved.etag) throw new CatalogError('INVALID_NOT_MODIFIED', 304)
        const checkedAt = Date.now()
        this.remember(url, { ...saved, checkedAt })
        return { data: parse(saved.data), freshness: 'revalidated', checkedAt }
      }
      if (!response.ok) throw new CatalogError(`HTTP_${response.status}`, response.status)
      const bytes = await bounded(response, JSON_LIMIT)
      let value: unknown
      try { value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)) } catch { throw new CatalogError('INVALID_RESPONSE') }
      let data: T
      try { data = parse(value) } catch { throw new CatalogError('INVALID_RESPONSE') }
      const checkedAt = Date.now()
      if (options.cache) this.remember(url, { data, etag: response.headers.get('ETag'), checkedAt, bytes: bytes.length })
      return { data, freshness: 'network', checkedAt }
    } catch (error) {
      const transient = !(error instanceof CatalogError) || error.status >= 500
      if (options.fallback && saved && transient) return { data: parse(saved.data), freshness: 'cached', checkedAt: saved.checkedAt }
      throw error
    } finally { clearTimeout(timeout) }
  }
  private remember(url: string, value: Cached): void {
    this.cache.delete(url); this.cache.set(url, value)
    while (this.cache.size > 16 || Array.from(this.cache.values()).reduce((sum, item) => sum + item.bytes, 0) > 16 * 1024 * 1024) this.cache.delete(this.cache.keys().next().value!)
  }
  page(url: string) { return this.request(url, parsePage) }
  sources() { return this.request('/catalog/v1/sources', parseSources, { cache: false, fallback: false }) }
  release(id: string, freshOnly = false) { return this.request(`/catalog/v1/releases/${encodeURIComponent(id)}`, parseRelease, { cache: !freshOnly, fallback: !freshOnly }) }
  async download(reviewed: Release, reviewedKey: string): Promise<Blob> {
    const [metadata, source] = await Promise.all([this.release(reviewed.release_id, true), this.sources()])
    const current = metadata.data, reason = availability(current, source.data)
    if (reason) throw new CatalogError(reason, 410)
    const key = source.data.keys.find(item => item.key_id === current.key_id && item.namespace === current.namespace)!
    const payload = (release: Release) => JSON.stringify(Object.entries(release).filter(([name]) => !['withdrawn', 'download_path'].includes(name)).sort(([a], [b]) => a.localeCompare(b)))
    if (payload(current) !== payload(reviewed) || key.public_key !== reviewedKey) throw new CatalogError('METADATA_CHANGED', 409)
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 30000)
    try {
      const response = await this.fetcher(current.download_path, { credentials: 'omit', cache: 'no-store', signal: controller.signal })
      if (!response.ok) throw new CatalogError(`HTTP_${response.status}`, response.status)
      const bytes = await bounded(response, Math.min(current.size, 10 * 1024 * 1024))
      if (bytes.length !== current.size) throw new CatalogError('ARCHIVE_SIZE_MISMATCH')
      if (!globalThis.crypto?.subtle) throw new CatalogError('SECURE_CONTEXT_REQUIRED')
      const digest = await crypto.subtle.digest('SHA-256', new Uint8Array(bytes).buffer)
      const hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
      if (hash !== current.sha256) throw new CatalogError('ARCHIVE_HASH_MISMATCH')
      return new Blob([new Uint8Array(bytes).buffer], { type: 'application/zip' })
    } finally { clearTimeout(timeout) }
  }
}
export function message(error: unknown): string {
  const code = error instanceof Error ? error.message : ''
  const messages: Record<string, string> = {
    HTTP_404: '没有找到这个发行，请检查链接或重新选择版本。', HTTP_410: '发行已撤回或签名公钥已撤销，请刷新状态。',
    INVALID_RESPONSE: '目录返回的数据格式不正确。', RESPONSE_TOO_LARGE: '返回内容超过读取限制。',
    METADATA_CHANGED: '元数据或公钥已变化，请刷新并重新确认。', ARCHIVE_SIZE_MISMATCH: '归档大小与发行记录不符，已取消下载。',
    ARCHIVE_HASH_MISMATCH: '归档 SHA-256 与发行记录不符，已取消下载。',
    SECURE_CONTEXT_REQUIRED: '下载校验需要 HTTPS 或 localhost。',
  }
  return messages[code] || (error instanceof CatalogError && error.status === 410 ? code : '暂时无法读取服务，请检查连接后重试。')
}
