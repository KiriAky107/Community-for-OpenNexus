import { bounded, CatalogError } from './api.ts'
import { object, parseDetail, parseItem, parsePage, parsePreview, parseReceipt, parseReports, parseReviews, parseSession, parseSessions, validateReceipt } from './workbenchTypes.ts'
import type { Detail, Intent, Session } from './workbenchTypes.ts'
import { parseMetadata } from './types.ts'
import type { Metadata } from './types.ts'

export class WorkbenchClient {
  private fetcher: typeof fetch
  private generation = 0
  private controllers = new Set<AbortController>()
  private checking: Promise<void> | null = null
  private current: Session | null = null
  private changed: (session: Session | null, reason?: string) => void
  private timeout: number
  constructor(fetcher: typeof fetch = fetch, changed: (session: Session | null, reason?: string) => void = () => {}, timeout = 30000) { this.fetcher = fetcher.bind(globalThis); this.changed = changed; this.timeout = timeout }
  get session() { return this.current }
  clear(reason?: string) { this.generation++; this.current = null; this.checking = null; for (const controller of this.controllers) controller.abort(); this.controllers.clear(); this.changed(null, reason) }
  private active(ticket: number, controller: AbortController) { if (ticket !== this.generation || controller.signal.aborted) throw new CatalogError('STALE_RESPONSE') }
  private async verify(): Promise<void> {
    if (!this.current) throw new CatalogError('AUTH_REQUIRED', 401)
    if (this.checking) return this.checking
    const original = this.current
    const promise = (async () => {
      const value = await this.request('/catalog/v1/web/session', parseSession, undefined, false)
      if (value.session_id !== original.session_id || value.principal_id !== original.principal_id || value.role !== original.role || value.namespace !== original.namespace) {
        this.clear('SESSION_CHANGED'); throw new CatalogError('SESSION_CHANGED', 401)
      }
      this.current = value; this.changed(value)
    })()
    this.checking = promise
    try { await promise } finally { if (this.checking === promise) this.checking = null }
  }
  private async request<T>(path: string, parse: (value: unknown) => T, body?: unknown, authenticated = true): Promise<T> {
    const ticket = this.generation, controller = new AbortController()
    this.controllers.add(controller)
    let timer: ReturnType<typeof setTimeout> | undefined
    const deadline = new Promise<never>((_, reject) => { timer = setTimeout(() => { controller.abort(); reject(new CatalogError('REQUEST_TIMEOUT')) }, this.timeout) })
    const work = (async () => {
      if (authenticated) await this.verify()
      this.active(ticket, controller)
      const headers: Record<string, string> = { Accept: 'application/json' }
      if (body !== undefined) {
        headers['Content-Type'] = 'application/json'
        if (authenticated) headers['X-Community-CSRF'] = this.current!.csrf
      }
      const response = await this.fetcher(path, { method: body === undefined ? 'GET' : 'POST', body: body === undefined ? undefined : JSON.stringify(body), headers, signal: controller.signal, credentials: 'same-origin', cache: 'no-store', redirect: 'error' })
      this.active(ticket, controller)
      if (response.status === 401) { this.clear('AUTH_REQUIRED'); throw new CatalogError('AUTH_REQUIRED', 401) }
      const bytes = await bounded(response, 8 * 1024 * 1024)
      this.active(ticket, controller)
      let value: unknown
      try { value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)) } catch { throw new CatalogError('INVALID_RESPONSE', response.status) }
      if (!response.ok) {
        const code = object(value).error
        const raw = code && typeof code === 'object' ? (code as Record<string, unknown>).code : ''
        const safe = typeof raw === 'string' && /^[A-Z][A-Z0-9_]{0,79}$/.test(raw) ? raw : `HTTP_${response.status}`
        throw new CatalogError(safe, response.status)
      }
      try { return parse(value) } catch { throw new CatalogError('INVALID_RESPONSE') }
    })()
    try { return await Promise.race([work, deadline]) } finally { clearTimeout(timer); this.controllers.delete(controller) }
  }
  async connect(token?: string): Promise<Session> {
    const value = await this.request('/catalog/v1/web/session', parseSession, token === undefined ? undefined : { token }, false)
    this.current = value; this.changed(value); return value
  }
  async logout() { await this.request('/catalog/v1/web/session/logout', object, {}); this.clear() }
  sessions() { return this.request('/catalog/v1/web/sessions', parseSessions) }
  async revokeSession(id: string) {
    const value = await this.request(`/catalog/v1/web/sessions/${encodeURIComponent(id)}/revoke`, object, {})
    if (value.state !== 'revoked' || value.session_id !== id) throw new CatalogError('INVALID_RESPONSE')
    if (this.current?.session_id === id) this.clear()
  }
  page(moderation = false, offset = 0) { return this.request(`/catalog/v1/${moderation ? 'moderation/reviews' : 'publish/submissions'}?offset=${offset}&limit=20`, moderation ? parseReviews : parsePage) }
  status(id: string) { return this.request(`/catalog/v1/publish/submissions/${encodeURIComponent(id)}`, parseItem) }
  detail(id: string, offset = 0) { return this.request(`/catalog/v1/publish/submissions/${encodeURIComponent(id)}/inspection?offset=${offset}&limit=100`, parseDetail) }
  preflight(release: Metadata, archive_base64: string) { return this.request('/catalog/v1/publish/preflight', parsePreview, { release, archive_base64 }) }
  reports(after = 0) { return this.request(`/catalog/v1/moderation/reports?after=${after}&limit=20`, parseReports) }
  async perform(intent: Intent) {
    if (this.current?.principal_id !== intent.principal_id) throw new CatalogError('SESSION_CHANGED', 401)
    const value = await this.request(intent.path, parseReceipt, intent.body)
    try { return validateReceipt(value, intent) } catch { throw new CatalogError('RECEIPT_MISMATCH') }
  }
  async reconcile(intent: Intent) {
    if (this.current?.principal_id !== intent.principal_id) throw new CatalogError('SESSION_CHANGED', 401)
    const value = await this.request(`/catalog/v1/web/operations/${intent.operation_id}`, parseReceipt)
    try { return validateReceipt(value, intent) } catch { throw new CatalogError('RECEIPT_MISMATCH') }
  }
  async archive(reviewed: Detail): Promise<Blob> {
    const ticket = this.generation, controller = new AbortController()
    this.controllers.add(controller)
    let timer: ReturnType<typeof setTimeout> | undefined
    const deadline = new Promise<never>((_, reject) => { timer = setTimeout(() => { controller.abort(); reject(new CatalogError('REQUEST_TIMEOUT')) }, this.timeout) })
    const work = (async () => {
      const current = await this.detail(reviewed.submission_id)
      if (current.inspection.review_digest !== reviewed.inspection.review_digest) throw new CatalogError('REVIEW_CHANGED', 409)
      await this.verify()
      this.active(ticket, controller)
      const response = await this.fetcher(`/catalog/v1/publish/submissions/${encodeURIComponent(reviewed.submission_id)}/archive`, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal })
      this.active(ticket, controller)
      if (!response.ok) { if (response.status === 401) this.clear('AUTH_REQUIRED'); throw new CatalogError(`HTTP_${response.status}`, response.status) }
      const bytes = await bounded(response, Math.min(current.release.size, 10 * 1024 * 1024))
      this.active(ticket, controller)
      if (bytes.length !== current.release.size) throw new CatalogError('ARCHIVE_SIZE_MISMATCH')
      const digest = await crypto.subtle.digest('SHA-256', new Uint8Array(bytes).buffer)
      this.active(ticket, controller)
      if (Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('') !== current.release.sha256) throw new CatalogError('ARCHIVE_HASH_MISMATCH')
      return new Blob([new Uint8Array(bytes).buffer], { type: 'application/zip' })
    })()
    try { return await Promise.race([work, deadline]) } finally { clearTimeout(timer); this.controllers.delete(controller) }
  }
}

export function freezeIntent(session: Session, kind: Intent['kind'], target: string, label: string, path: string, body: Record<string, unknown>): Intent {
  const operation_id = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
  // Preserve the reviewed payload even if the original form changes later.
  const frozen = JSON.parse(JSON.stringify({ ...body, operation_id })) as Record<string, unknown>
  function freeze(value: unknown) { if (value && typeof value === 'object') { for (const child of Object.values(value)) freeze(child); Object.freeze(value) } }
  freeze(frozen)
  return Object.freeze({ principal_id: session.principal_id, kind, target, label, path, operation_id, body: frozen })
}
export async function localPackage(metadataFile: File, archiveFile: File): Promise<{ release: Metadata; archive_base64: string }> {
  if (metadataFile.size > 1024 * 1024 || archiveFile.size > 10 * 1024 * 1024 || !archiveFile.size) throw new CatalogError('INPUT_TOO_LARGE')
  let release: Metadata
  try {
    const value = object(JSON.parse(await metadataFile.text()))
    const fields = ['schema_version', 'namespace', 'package_id', 'type', 'version', 'name', 'author_id', 'license', 'description', 'sha256', 'size', 'platforms', 'architectures', 'min_app_version', 'max_app_version', 'dependencies', 'permissions', 'changelog', 'published_at', 'key_id', 'signature']
    if (Object.keys(value).some(key => !fields.includes(key))) throw new Error('UNEXPECTED_FIELD')
    release = parseMetadata(value)
  } catch { throw new CatalogError('INVALID_METADATA') }
  const bytes = new Uint8Array(await archiveFile.arrayBuffer())
  if (bytes.length !== release.size) throw new CatalogError('ARCHIVE_SIZE_MISMATCH')
  const hash = await crypto.subtle.digest('SHA-256', bytes.buffer)
  if (Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('') !== release.sha256) throw new CatalogError('ARCHIVE_HASH_MISMATCH')
  let binary = ''
  for (let offset = 0; offset < bytes.length; offset += 32768) binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768))
  return { release, archive_base64: btoa(binary) }
}
export function workbenchMessage(error: unknown): string {
  const code = error instanceof Error ? error.message : ''
  const messages: Record<string, string> = {
    AUTH_REQUIRED: '会话已失效，请重新登录后核对原操作。', SESSION_CHANGED: '身份或角色已变化，请重新确认会话。', WEB_ORIGIN_REQUIRED: '管理员需要配置 COMMUNITY_WEB_ORIGIN 才能启用工作台登录。', WEB_ORIGIN_MISMATCH: '网页来源与服务配置不一致，请从管理员指定的同源地址进入。',
    SESSION_LIMIT: '已达到八个有效会话，请在已登录页面撤销旧会话或等待到期。', SESSION_ALREADY_ACTIVE: '浏览器已有会话，请重新确认会话。', LOGIN_RATE_LIMIT: '登录尝试过多，请稍后再试。', CSRF_REQUIRED: '会话证明已变化，请重新确认会话。', ROLE_REQUIRED: '当前身份没有执行该操作的角色。', OWNERSHIP_REQUIRED: '当前身份没有访问这个提交的权限。', NAMESPACE_OWNERSHIP: '签名元数据的作者或命名空间与当前身份不一致。', UNTRUSTED_SIGNER: '签名公钥未登记或已撤销，无法提交或批准。', SELF_REVIEW_FORBIDDEN: '作者不能审核自己的提交，请由独立审核员处理。',
    IMMUTABLE_VERSION: '该版本已经提交，不能替换。请查看原状态或准备新的版本。', REVIEW_CHANGED: '检查内容已变化，请刷新详情后重新审核。', REVIEW_ALREADY_CLOSED: '这个提交已由另一操作处理，请刷新状态。', REPORT_ALREADY_RESOLVED: '这条举报已经处理，请刷新队列。', RELEASE_ALREADY_WITHDRAWN: '发行已撤回，请刷新状态。',
    PACKAGE_VALIDATION_FAILED: '签名、归档或类型清单校验未通过，请检查本地文件。', INVALID_METADATA: '请选择有效的已签名发行 JSON，不要上传私钥。', INPUT_TOO_LARGE: '元数据应不超过 1 MiB，ZIP 应不超过 10 MiB。', ARCHIVE_SIZE_MISMATCH: '归档大小与签名记录不符。', ARCHIVE_HASH_MISMATCH: '归档 SHA-256 与签名记录不符。',
    INVALID_RESPONSE: '服务响应格式不正确，请核对服务状态。', RECEIPT_MISMATCH: '返回回执与原操作不匹配，结果待核对。', OPERATION_ID_REUSED: '原编号已用于不同内容，请先核对原操作。', REQUEST_TIMEOUT: '等待已超时，写入结果可能尚未确认。',
  }
  return messages[code] || '暂时无法确认服务结果，请检查连接后重试读取。'
}
